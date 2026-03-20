# F-002: Proactive Time-Aware Nudges

**Priority:** P0 — Must Have
**Status:** Done
**Depends on:** Calendar (done), TTS (done), travel-time alerts (done), APScheduler (done)
**Blocks:** None (standalone enhancement)

---

## ADHD Insight

Time blindness is one of the most disabling ADHD traits. A meeting in 30 minutes feels like "later" until it's 2 minutes away. External cues help the brain perceive time.

## What Exists Today

- Calendar polling every 15 min, announces events within 2 hours
- Travel-time alerts with Google Maps ("leave in X minutes")
- Morning briefing at 7AM with today's events

## What's Missing

1. **Tiered countdown alerts** — not just one announcement, but a sequence: 1hr → 30min → 15min → 5min
2. **Transition scaffolding** — instead of just "meeting in 5 min," say "Meeting in 5 — save your work, grab water, pull up the agenda"
3. **Smart suppression** — don't announce the same event 4 times if you're in a related focus session

---

## Implementation

### Modified File: `orchestrator/background_jobs.py`

Enhance `poll_calendar()` to support tiered announcements:

```python
# New state tracking
_event_announcements: dict[str, set[int]] = {}
# event_id → set of thresholds already announced (60, 30, 15, 5)

COUNTDOWN_TIERS = [
    {"minutes": 60, "message": "You have {title} in about an hour."},
    {"minutes": 30, "message": "{title} in 30 minutes. Start wrapping up what you're doing."},
    {"minutes": 15, "message": "{title} in 15 minutes. Time to transition — save your work, grab water."},
    {"minutes": 5,  "message": "{title} starts in 5 minutes. {prep_hint}"},
]

async def poll_calendar_tiered():
    """Enhanced calendar polling with tiered countdown.
    For each upcoming event, check which tier thresholds have been crossed
    since last poll. Announce the most recent un-announced tier.
    Skip announcements if focus session is active and related to the event.
    Travel-time events use leave-by time instead of event start."""
```

### Prep Hints (5-minute tier)

Generate contextual prep based on event metadata:

```python
def _get_prep_hint(event: CalendarEvent) -> str:
    """Generate transition scaffolding hint.
    - If event has a Zoom/Teams link: 'Pull up the meeting link.'
    - If event has a location: 'Head out — it's a {drive_time} minute drive.'
    - If event has a description with agenda: 'Check the agenda.'
    - Default: 'Take a breath, you've got this.'
    """
```

### Modified Files

| File | Changes |
|------|---------|
| `background_jobs.py` | Replace `poll_calendar()` with `poll_calendar_tiered()`, add `_event_announcements` state |
| `shared.py` | Add tier config env vars |

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CALENDAR_TIERED_ALERTS` | `true` | Enable tiered countdown (vs single announcement) |
| `CALENDAR_ALERT_TIERS` | `60,30,15,5` | Minutes before event for each tier |

---

## Testing Checklist

- [ ] Event at T-65min: no announcement yet
- [ ] Event at T-58min: 60-minute tier fires
- [ ] Event at T-29min: 30-minute tier fires (60 not repeated)
- [ ] Event at T-14min: 15-minute tier fires with transition language
- [ ] Event at T-4min: 5-minute tier fires with prep hint
- [ ] Travel-time event uses leave-by time for tiers
- [ ] Zoom event gets "pull up the meeting link" prep hint
- [ ] Focus session suppresses alerts (or softens them)
- [ ] Orchestrator restart clears announcement state (re-announces — acceptable)
