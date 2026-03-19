# F-008: Meal & Self-Care Nudges

**Priority:** P2 — Nice to Have
**Status:** Not started
**Depends on:** TTS (done), APScheduler (done), data manager (done), HA (done), F-006 Routine Scaffolding
**Blocks:** F-009 Decision Simplifier (provides self-care state for triage)

---

## ADHD Insight

Hyperfocus causes ADHDers to forget basic needs — eating, drinking water, standing up, taking meds. The brain literally doesn't send the signal when locked onto something interesting.

## What Jess Does

Monitors time since last meal, medication schedule, and sitting time. Nudges at appropriate intervals via TTS. Gentle external signals, not nagging.

## Interaction Examples

```
Jess: "It's 1:30 and you haven't eaten since breakfast.
       Want me to suggest something quick?"

Jess: "Hey, did you take your afternoon meds?"
User: "Hey Jess, yes"
Jess: "Logged. Next dose is evening."

Jess: "You've been at your desk for 90 minutes. Stand up and
       stretch for 2 minutes."
```

---

## Implementation

### New File: `orchestrator/selfcare_manager.py`

```python
@dataclass
class SelfCareState:
    last_meal_reported: datetime | None
    last_hydration_nudge: datetime | None
    last_movement_nudge: datetime | None
    last_med_confirmation: dict[str, datetime]  # med_name → last confirmed
    sitting_since: datetime | None

MEAL_NUDGE_HOURS = 4
HYDRATION_INTERVAL_MIN = 90
MOVEMENT_INTERVAL_MIN = 90

async def check_selfcare() -> list[str]
    """Called by APScheduler every 15 min. Returns nudge messages.
    - If > MEAL_NUDGE_HOURS since last_meal_reported: meal nudge
    - If > HYDRATION_INTERVAL_MIN since last nudge: water nudge
    - If > MOVEMENT_INTERVAL_MIN since last nudge: movement nudge
    - If med window open and not confirmed: med nudge
    Respects quiet hours and focus sessions."""

async def confirm_meal(meal_type: str = "meal") -> dict
async def confirm_med(med_name: str) -> dict
async def confirm_hydration() -> dict
async def get_selfcare_status() -> dict
```

### Tool

```json
{
  "name": "selfcare_log",
  "description": "Log a self-care action: meal eaten, medication taken, water drunk, or movement done.",
  "parameters": {
    "type": "object",
    "properties": {
      "action": { "type": "string", "enum": ["meal", "medication", "water", "movement"] },
      "detail": { "type": "string", "description": "Medication name or meal type" }
    },
    "required": ["action"]
  }
}
```

### Medication Schedule Integration

Load from existing YAML data (`data_manager.py`):

```yaml
# Already exists in data/medications.yaml
medications:
  - name: "Adderall"
    dose: "20mg"
    schedule: "morning"      # → check window 7:00-9:00
  - name: "Melatonin"
    dose: "5mg"
    schedule: "evening"      # → check window 21:00-22:00
```

### TTS Templates

```
Meal:       "It's {time} and you haven't eaten since {last}. Grab something — even a snack."
Water:      "Water check. Take a few sips."
Movement:   "You've been sitting for {minutes} minutes. Stand up and stretch."
Meds:       "Hey, did you take your {med_name}?"
Med logged: "Logged. Next dose is {next_schedule}."
```

### Smart Suppression

- Don't nudge during active focus session (or soften: "After this sprint, grab water")
- Don't nudge during active routine (routine handles meds)
- Respect quiet hours
- Max one nudge per check cycle (don't stack meal + water + movement)

### Modified Files

| File | Changes |
|------|---------|
| `tool_definitions.py` | Add `selfcare_log` schema |
| `tool_handlers.py` | Add handler, dispatch |
| `background_jobs.py` | Add `check_selfcare()` scheduled job |
| `shared.py` | Add env vars |

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SELFCARE_ENABLED` | `true` | Enable self-care nudges |
| `MEAL_NUDGE_HOURS` | `4` | Hours since last meal before nudging |
| `HYDRATION_INTERVAL` | `90` | Minutes between water reminders |
| `MOVEMENT_INTERVAL` | `90` | Minutes between movement reminders |
| `QUIET_HOURS_START` | `22:00` | No nudges after this |
| `QUIET_HOURS_END` | `07:00` | No nudges before this |

---

## Testing Checklist

- [ ] Meal nudge fires after configured hours
- [ ] Med nudge fires during medication window
- [ ] "Yes I took my meds" logs and stops nudging
- [ ] Water nudge fires at interval
- [ ] Movement nudge fires at interval
- [ ] Suppressed during focus session
- [ ] Suppressed during quiet hours
- [ ] No stacked nudges (one per cycle)
- [ ] Medication schedule loaded from existing YAML
