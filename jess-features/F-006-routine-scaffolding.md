# F-006: Context-Aware Routine Scaffolding

**Priority:** P1 — Should Have
**Status:** Done
**Depends on:** TTS (done), APScheduler (done), Google Calendar (done), HA (done)
**Blocks:** F-005 Progress Tracking, F-008 Self-Care Nudges

---

## ADHD Insight

Routines are extremely hard for ADHD brains to build and maintain. Step-by-step guidance — like a GPS for your day — keeps things on track.

## What Jess Does

Walks you through routines step by step via TTS. One step at a time, waits for you, nudges if you go quiet, adapts if you're running late against your calendar. Not a checklist — a living guide.

## Interaction Flow

### Morning — Happy Path

```
7:00 AM — APScheduler triggers OR user says "Hey Jess, I'm up"

Jess: "Morning, Nadim. First up: take your meds. Let me know when you're done."
  [User: "Hey Jess, done"]
Jess: "Got it. Shower's next. Your first meeting is at 10, so no rush."
  [10 min silence]
Jess: "Still on the shower? No pressure, just checking."
  [User: "Hey Jess, done"]
Jess: "Nice. Time for breakfast. You've got about 40 minutes."
  [User: "Hey Jess, skip"]
Jess: "Skipped. Last step: check your calendar and top task."
  [User: "done"]
Jess: "Morning routine done. Standup at 10, clear afternoon. Have a good one."
```

### Morning — Running Late

```
7:30 AM — User says "Hey Jess, I'm up" (30 min late)

Jess: "Morning. You're running a bit behind — first meeting at 9.
       Take your meds first, that's the priority."
  [User: "done"]
Jess: "Quick shower — you've got 50 minutes. I'd skip full breakfast
       and grab something quick after."
```

### Evening

```
9:00 PM — Scheduled trigger

Jess: "Wind-down time. First: take your evening meds."
  [User: "done"]
Jess: "Quick look at tomorrow — dentist at 11, clear afternoon."
  [User: "done"]
Jess: "Last thing: screens away. Goodnight, Nadim."
```

### Voice Commands During Routine

| Command | Action |
|---------|--------|
| "done" / "next" / "finished" | Advance to next step |
| "skip" | Skip current step |
| "what's next" | Repeat current step |
| "how am I on time" | Time check against calendar |
| "pause routine" | Pause nudges |
| "resume routine" | Resume nudges |
| "stop routine" | End early |

---

## Implementation

### New File: `orchestrator/routine_manager.py`

```python
@dataclass
class RoutineSession:
    routine_id: str                # "morning" / "evening"
    started_at: datetime
    current_step_index: int
    step_started_at: datetime
    skipped_steps: list[str]
    completed_steps: list[str]
    paused: bool
    nudge_count: int
    deadline_event: str | None
    deadline_time: datetime | None
    speaker: str

_active_session: RoutineSession | None = None
_routines: dict = {}

async def load_routines(yaml_path: str) -> None
async def start_routine(routine_id: str, speaker: str = None) -> dict
async def advance_step(action: str = "done") -> dict
async def nudge_current_step() -> None
async def pause_routine() -> dict
async def resume_routine() -> dict
async def stop_routine() -> dict
async def get_routine_status() -> dict
def _calculate_buffer_minutes() -> int | None
def _build_step_announcement(step, buffer, is_late) -> str
```

### Data File: `data/routines.yaml`

```yaml
routines:
  morning:
    display_name: "Morning Routine"
    trigger:
      type: scheduled
      time: "07:00"
      days: [mon, tue, wed, thu, fri, sat, sun]
    speaker: "media_player.bedroom_pair"
    nudge_delay_minutes: 10
    steps:
      - id: meds
        label: "Take your meds"
        est_minutes: 2
        skippable: false
        ha_action:
          entity_id: "light.bedroom"
          service: "turn_on"
          data: { brightness: 200, color_temp_kelvin: 4000 }
      - id: shower
        label: "Shower"
        est_minutes: 15
        skippable: true
      - id: breakfast
        label: "Eat breakfast"
        est_minutes: 20
        skippable: true
        fallback_label: "Grab something quick"
        fallback_threshold_minutes: 30
      - id: calendar_check
        label: "Check your calendar and top task"
        est_minutes: 5
        skippable: true
        include_calendar_summary: true

  evening:
    display_name: "Evening Routine"
    trigger:
      type: scheduled
      time: "21:00"
    speaker: "media_player.bedroom_pair"
    nudge_delay_minutes: 15
    steps:
      - id: evening_meds
        label: "Take your evening meds"
        est_minutes: 2
        skippable: false
      - id: tomorrow_prep
        label: "Quick look at tomorrow's calendar"
        est_minutes: 3
        skippable: true
        include_calendar_summary: true
        calendar_days_ahead: 1
      - id: screens_off
        label: "Screens away"
        est_minutes: 0
        skippable: true
```

### Tools

```json
{ "name": "start_routine", "parameters": { "routine_id": { "enum": ["morning", "evening"] } } }
{ "name": "routine_action", "parameters": { "action": { "enum": ["done", "skip", "pause", "resume", "stop"] } } }
{ "name": "routine_status", "parameters": {} }
```

Add `start_routine`, `routine_action` to `TERMINAL_TOOLS`.

### TTS Templates

```
First step:     "{Greeting}, Nadim. First up: {label}. Let me know when you're done."
                (Greeting = Morning 04:00-11:59 / Afternoon 12:00-16:59 / Evening otherwise,
                 chosen by _greeting_word(hour) in routine_manager.py)
Middle step:    "Got it. Next: {label}."
Middle + time:  "Nice. {label} next. About {buffer} minutes before {event}."
Late + fallback:"Running behind. {fallback_label} — need to be ready by {time}."
Last step:      "Almost done. Last thing: {label}."
Nudge 1:        "Still on {label}? No pressure, just checking."
Nudge 2:        "Still working on {label}? Take your time, or say 'skip'."
Nudge 3:        "I'll skip {label} in a couple minutes unless you say otherwise."
Complete:       "Morning routine done. {calendar_summary}. Have a good one."
```

### No Always-On Mic Required

Start: APScheduler → TTS. Advance: "Hey Jess" wake word. Nudges: APScheduler → TTS. Optional: ATOM Echo button mapped to "advance step" via HA automation.

### Modified Files

| File | Changes |
|------|---------|
| `tool_definitions.py` | Add 3 tool schemas |
| `tool_handlers.py` | Add handlers, dispatch, `TERMINAL_TOOLS` |
| `background_jobs.py` | Add scheduled routine triggers |
| `orchestrator.py` | Load `routines.yaml` at startup |
| `prompt_builder.py` | Inject active routine context |

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `ROUTINES_YAML_PATH` | `/app/data/routines.yaml` | Routine definitions |
| `ROUTINE_ENABLED` | `true` | Enable scheduled triggers |
| `ROUTINE_NUDGE_MAX` | `3` | Max nudges per step |
| `ROUTINE_AUTO_SKIP` | `false` | Auto-skip after max nudges |

### Nudge exhaustion behavior

Once `nudge_count > ROUTINE_NUDGE_MAX`, `_deliver_nudge` force-ends the active routine (logged at WARNING) in either case:

- the current step is non-skippable (regardless of `ROUTINE_AUTO_SKIP`), or
- `ROUTINE_AUTO_SKIP` is off

This prevents stuck evening/morning routines from nudging indefinitely when the user has gone to bed or walked away. When `ROUTINE_AUTO_SKIP=true` and the step is skippable, the original auto-skip path still runs.

### Selfcare bridge (bidirectional)

**Selfcare -> routine:** `selfcare_log({action})` fires `_maybe_advance_routine_for_action` as a fire-and-forget async task. If an active routine's current step matches `action` via word-boundary regex on `step.id`/`step.label` (keyword map: `medication→{meds,medication,medications}`, `meal→{meal,breakfast,lunch,dinner,eat}`, `water→{water,hydrate,hydration}`, `movement→{movement,stretch,walk,exercise}`), the routine advances with `advance_step("done")`. Errors are logged at ERROR with `exc_info`; failures never block the selfcare write.

**Routine -> selfcare (reverse):** after `advance_step("done")` appends to `completed_steps`, it calls `selfcare_manager.mark_selfcare_from_routine_step(step)` synchronously. This uses `_infer_selfcare_action(step)` (same keyword map as above) to dispatch to `record_medication_logged` / `record_meal_logged` / `record_hydration_logged` / `record_movement_logged`, which suppress the corresponding nudge. Routine-sourced medication logging unconditionally sets the generic `last_med_confirmation["medication"]` key (routine labels like `'routine:meds'` can't be mapped to a morning/evening med window). Only fires on `"done"` — NOT on `"skip"` or auto-end `"stop"` (skipped meds shouldn't mark as taken). Wrapped in try/except with `logger.error(exc_info=True)`; never blocks advance.

---

## Testing Checklist

- [ ] Morning routine starts via scheduled trigger and via voice
- [ ] Steps advance with "done", "skip"
- [ ] Non-skippable steps reject skip (meds)
- [ ] Silence nudge fires after configured delay
- [ ] Calendar-aware time context in announcements
- [ ] Fallback labels when running late
- [ ] Pause/resume stops and restarts nudges
- [ ] HA actions fire on step start (lights)
- [ ] Evening routine works independently
- [ ] Cannot start routine while one is active
