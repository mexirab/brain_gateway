# F-005: Dopamine-Aware Progress Tracking

**Priority:** P1 — Should Have
**Status:** Done
**Depends on:** F-001 Brain Dump, F-003 Task Decomposition, F-004 Focus Sessions, F-006 Routine Scaffolding
**Blocks:** F-009 Decision Simplifier (provides context for decisions)

---

## ADHD Insight

ADHD brains need frequent, immediate reward signals. Long-term progress is invisible and unmotivating. Visual progress and micro-celebrations create dopamine hits that sustain momentum.

## What Jess Does

Tracks completed tasks and delivers satisfying progress feedback — pushed via TTS and dashboard. Streaks, daily counts, personal bests. Never buried in an app you'd need to open.

## Interaction Examples

```
# End of day (pushed via TTS at ~6PM)
Jess: "Quick daily recap: you completed 7 tasks, worked 2 focus sessions
       totaling 85 minutes, and captured 3 brain dumps. That's your best
       Wednesday this month."

# Streak milestone (pushed when triggered)
Jess: "You've captured a brain dump 5 days in a row. Streak!"

# Weekly digest (Sunday evening)
Jess: "Your week: 34 tasks done, 8 focus sessions, 12 brain dumps.
       Tuesday was your most productive day. Trending up from last week."
```

---

## Implementation

### New File: `orchestrator/progress_tracker.py`

```python
@dataclass
class DailyStats:
    date: str                     # YYYY-MM-DD
    tasks_completed: int
    brain_dumps_captured: int
    focus_sessions: int
    focus_minutes: int
    reminders_completed: int
    routine_steps_completed: int
    streaks: dict[str, int]       # streak_name → consecutive days

# Persistence: SQLite (same pattern as finance.db)
# Location: /app/data/progress.db

async def record_event(event_type: str, metadata: dict = {}) -> None
    """Called by other managers when something completes.
    event_type: task_done, brain_dump, focus_complete, reminder_done, routine_done
    Increments daily counters. Checks for streak milestones."""

async def check_streaks() -> list[str]
    """Check all streak categories. Return milestone messages.
    Streaks: brain_dump_streak, focus_streak, routine_streak.
    Milestones at: 3, 5, 7, 14, 30 days."""

async def daily_summary() -> str
    """TTS-friendly daily summary.
    Compares to same day of week average for 'personal best' detection."""

async def weekly_summary() -> str
    """Weekly digest. Best day, totals, trend vs prior week."""

def _personal_best_check(stat: str, value: int) -> str | None
    """Compare against historical data. Return celebration text or None."""
```

### Background Jobs (add to `background_jobs.py`)

```python
async def daily_progress_summary():
    """Triggered at 6PM daily. Announce daily stats via TTS."""

async def weekly_progress_digest():
    """Triggered Sunday 7PM. Announce weekly summary via TTS."""
```

### Integration Points

Other managers call `progress_tracker.record_event()` on completion:

```python
# brain_dump_manager.py → after brain_dump completes:
await progress_tracker.record_event("brain_dump", {"count": len(items)})

# focus_manager.py → after session ends:
await progress_tracker.record_event("focus_complete", {"minutes": total, "sprints": count})

# routine_manager.py → after routine completes:
await progress_tracker.record_event("routine_done", {"routine": id, "steps": completed})

# reminder_manager.py → after reminder completed:
await progress_tracker.record_event("reminder_done", {"text": reminder_text})
```

### TTS Templates

```
Daily:         "Daily recap: {summary}. {personal_best_or_encouragement}"
Streak:        "{streak_name} streak: {count} days in a row!"
Weekly:        "Your week: {summary}. {trend}."
Personal best: "That's your best {day_of_week} this month."
Encouragement: "Solid day." / "Momentum building." / "Consistent beats intense."
```

### Dashboard Widget: `ProgressCard.tsx`

- Daily task count with mini bar chart (last 7 days)
- Active streaks with flame icons
- Weekly trend arrow (up/down/flat)
- Pulls from `GET /api/progress/today` and `GET /api/progress/week`

### API Endpoints (add to `api_routes.py`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/progress/today` | Today's stats |
| GET | `/api/progress/week` | This week's stats + trend |
| GET | `/api/progress/streaks` | Active streaks |

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PROGRESS_ENABLED` | `true` | Enable progress tracking |
| `DAILY_SUMMARY_TIME` | `18:00` | When to announce daily summary |
| `WEEKLY_SUMMARY_DAY` | `sunday` | Day for weekly digest |
| `WEEKLY_SUMMARY_TIME` | `19:00` | Time for weekly digest |

### Modified Files

| File | Changes |
|------|---------|
| `background_jobs.py` | Add daily/weekly summary jobs |
| `orchestrator.py` | Initialize SQLite DB at startup |
| `api_routes.py` | Add progress endpoints |
| Other managers | Add `record_event()` calls at completion points |

---

## Testing Checklist

- [ ] Events recorded correctly from each source
- [ ] Daily counter increments per event type
- [ ] Daily summary announces correct totals at 6PM
- [ ] Streak detection: 3, 5, 7 day milestones trigger announcement
- [ ] Streak breaks correctly after a missed day
- [ ] Personal best compares to same day of week
- [ ] Weekly summary totals accurate
- [ ] Week-over-week trend calculation works
- [ ] Dashboard widget renders daily/weekly data
- [ ] SQLite persists across orchestrator restarts
