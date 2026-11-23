# F-008: Meal & Self-Care Nudges

**Priority:** P2 — Nice to Have
**Status:** Done
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
- When a user logs a selfcare action during an active routine and the logged action matches the current step (medication / meal / water / movement keyword map), `log_selfcare` fires `routine_manager._maybe_advance_routine_for_action` fire-and-forget and the routine auto-advances. See F-006 → "Selfcare bridge".
- **Routine completion suppresses the matching nudge (reverse bridge):** when `routine_action("done")` advances past a step whose id/label matches medication/meal/water/movement, `routine_manager` calls `selfcare_manager.mark_selfcare_from_routine_step(step)` synchronously, which invokes the relevant `record_*_logged` helper. The morning-meds step sets the generic `last_med_confirmation["medication"]` key so `_check_meds` suppresses. Only fires on `"done"`, not `"skip"`/auto-`"stop"`.
- **Workout `log_set` counts as movement:** after persisting a set, `workout_manager.log_set` calls `selfcare_manager.record_movement_logged(f"set:{exercise_name}")`, resetting both `last_movement_nudge` and `sitting_since`. Closes the "sitting 274 min while at gym" complaint.
- Respect quiet hours
- Max one nudge per check cycle (don't stack meal + water + movement)

### Sitting clock starts with the movement hours (2026-11-23)

Applies to every install, no env var.

- The sitting time counts from max(`sitting_since`, start of the movement category's active hours) (`_sitting_start` / `_movement_day_start`). The start is 09:00 by default and set in `/settings` → Selfcare.
- Before: the midnight reset parked `sitting_since` at ~00:00, so the first nudge at 09:00 said "about 546 minutes". The night counted as sitting.
- With defaults (09:00–18:00, 90 min) the first movement nudge comes at ~10:30 instead of ~09:00, about one fewer nudge a day.
- `get_selfcare_status()["sitting_minutes"]` (read by the `selfcare_log` check action and `decide_for_me`) reads 0 before the window opens.
- No full active-hours window (start and end): unchanged. A window that wraps midnight counts from last night's start.

### Movement seen by the phone (default off)

The movement nudge only knew about movement someone logged, so it nagged after a walk. With `MOVEMENT_STEPS_ENTITY` (and optionally `MOVEMENT_STAND_ENTITY`) set, Jess reads the Home Assistant iPhone app's Apple Health sensors. The header comment of the "Movement seen by the phone" section in `orchestrator/selfcare_manager.py` is authoritative.

| Rule | Behaviour |
|------|-----------|
| When | Every `check_selfcare` cycle (15 min), `_refresh_movement_from_health(now)` runs before the away, quiet-hours, focus and routine gates, so walking during them still counts |
| How | HA REST `GET /api/states/<id>` over `shared._http` (5 s timeout). Not `ha_client`, which prints entity ids |
| Counts as movement | Steps gained ≥ `MOVEMENT_STEPS_THRESHOLD` (150) since the last read, or +1 Watch stand hour. Both sensors are since-midnight totals |
| Movement time | The sensor's `last_changed`, held between the baseline read and now |
| Effect | `record_movement_logged("health:steps"\|"health:stand", at=…)`: the sitting clock and nudge gate move forward (never back), the movement inbox card is resolved, and one `selfcare_log` row (no counts) is written at most every 30 min. In between, only the clock moves |
| No evidence (fail open toward nudging) | Unavailable or unknown, HTTP error, not a number, implausible (steps > 200000, stand > 24). The timer runs as it did without the feature |
| Drop within a day | Keeps the higher baseline until a second, different phone report confirms the lower count, so a dip that recovers can't fake a walk |
| New day | Only a count that went DOWN on a later day is a new day's total. The same or a higher count on a later day is a carry-over (HA restored yesterday's state after an overnight restart), not a walk |
| New-day steps | Walk time unknown. First reported before today's movement hours open: credited (the morning walk). Inside them: no credit |
| Restart | Baselines are RAM-only, so the first read only sets them |
| Logs | Never values or entity ids. One WARNING per new failure kind (the same kind repeats at most hourly; WARNINGs feed the self-audit), INFO on recovery |

Startup log line when on: `[SCHEDULER] Self-care nudges every 15 min (movement from the phone: steps, stand)`.

**Setup (owner):**

1. HA iPhone app: Settings → Companion App → Sensors → Apple Health Sensors → turn on Steps (and Stand Hours, BETA). Allow Health access when iOS asks. Use the Apple Health steps sensor (`sensor.<phone>_health_steps`, includes the Watch), not the plain pedometer `sensor.<phone>_steps` (phone-in-pocket steps only).
2. iPhone Settings → Home Assistant → Background App Refresh on. Without it the phone can silently stop reporting for weeks.
3. Jupiter `.env`: `MOVEMENT_STEPS_ENTITY=sensor.<phone>_health_steps` (optional `MOVEMENT_STAND_ENTITY=sensor.<phone>_stand_hours`), then `docker compose up -d --force-recreate orchestrator`. Needs `SELFCARE_ENABLED` and the movement category on.
4. Verify: the startup line above, `bgw_movement_health_read_total{result="ok"}` rising every 15 min, and after a short walk (and the phone's next report) a movement entry with detail `health:steps` in `/api/selfcare/today`.

**Monitoring:** `bgw_movement_health_read_total{kind,result}`, `bgw_movement_auto_detected_total{source}`, `bgw_movement_health_last_update_timestamp_seconds{kind}`; quiet alert `MovementSensorFailing`; Grafana row "Movement From the Phone". Runbook: `monitoring/README.md` → Prometheus Targets → Movement from the phone.

**Known limits:**

- The phone reports to HA about hourly by day (Nov 2026, 3 days: median gap 40 min, p90 ~2 h). A walk is credited when the phone reports it, so a nudge can still land between a walk and the report.
- A restart loses the baselines, so the first read only sets them. `sitting_since` restored from the last row can be up to 30 min older than the real last movement, which only means an earlier nudge.
- Travelling west of the server's timezone can switch detection off for that day. It fails safe: the plain timer.
- If the HA app stops reporting, Jess quietly falls back to the plain timer. The Grafana age panel and `MovementSensorFailing` make it visible.

Tests: `orchestrator/tests/test_movement_health.py`.

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
| `MOVEMENT_STEPS_ENTITY` | (empty = off) | HA Apple Health steps sensor; see Movement seen by the phone + `docs/ENV_VARS.md` |
| `MOVEMENT_STAND_ENTITY` | (empty = off) | Optional HA Apple Watch stand-hours sensor |
| `MOVEMENT_STEPS_THRESHOLD` | `150` | Steps gained between two checks that count as movement (20–5000) |
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
