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

Optional per-step `ha_action` (`{entity_id, service, data}`) fires via `shared.ha_client` when the step is announced (`routine_manager._fire_step_ha_action`). **Skipped while DND is active** (`shared.DND_ACTIVE`, set by `sleep_mode`) so a routine can't turn lights on during quiet time. The example above is illustrative; the live `data/routines.yaml` has no `ha_action` steps.

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

### Routine vs morning briefing

Both default to 07:00. Cast `play_media` replaces whatever is playing, so the briefing used to cut off the routine's first step. Now `morning_briefing` yields 1s before its announce (lets a routine started in the same scheduler pass set its session), then `jobs_calendar._routine_active_or_imminent()` checks:

- an active routine session (not paused, started < 3h ago), or
- a routine trigger job (`routines_config._is_routine_trigger_job`, excludes `routine_nudge_*`) firing within 120s.

If either holds, the spoken briefing is skipped, its text is mirrored off the speakers — dashboard inbox `message` card + Telegram via `jobs_calendar._mirror_message` (the missed-reminder recap is mirrored separately), and the parked item stays parked. Outcome counted as `bgw_morning_briefing_outcome_total{outcome="skipped_routine"}`. The evening briefing applies the same active-session gate (no imminent-trigger window): it skips speech, keeps the item parked, mirrors the digest (inbox card + Telegram) and counts `bgw_evening_briefing_outcome_total{outcome="skipped_routine"}`. If the routine check itself fails it logs a WARNING and speaks. Under DND it parks silently with no card.

**Known gap:** with the default 07:00 morning routine, the briefing is effectively inbox/Telegram-only every day (an inbox card in the `/chat` tray, Telegram if the bot is on). The routine does NOT speak weather, today's events, the parked item, or the missed-reminder recap. The `include_calendar_summary` step flag is parsed but unused; only `_complete_routine` speaks a calendar summary (Google-only, ≤3 events) and only if all steps finish.

### Metrics

`bgw_routine_started_total{routine,triggered_by}`, `bgw_routine_steps_advanced_total{routine,action}`, `bgw_routine_completed_total{routine}`, `bgw_routine_auto_skipped_total{routine,step}`, `bgw_routine_auto_ended_total{routine,step}` (the stuck-routine signal; Grafana-only alert `RoutineAutoEnded`), and `bgw_selfcare_logged_total{action}` for every self-care log, routine bridge included.

### Evening shutdown ritual (evening briefing)

`jobs_calendar.evening_briefing()`, daily cron at `EVENING_BRIEFING_TIME` (default 21:30; `EVENING_BRIEFING_ENABLED`). Registered in `orchestrator.py` whether or not a calendar is configured (meds + parking still deliver without one); registration is try/except-guarded so a malformed time can't crash-loop startup. The mirror of the morning briefing:

- **Tomorrow's first event** from every calendar source (`jobs_calendar.get_tomorrow_events()`, shared with the wind-down nudge) + a leave-by time (`get_travel_time` + `TRAVEL_TIME_BUFFER`) when it has a physical location. Work (Exchange) locations are never sent to Maps. A calendar it couldn't read is said out loud, never read as a clear day.
- **Evening meds check:** `selfcare_manager.evening_meds_status()`, same "confirmed today, hour ≥ 17" semantics as `_check_meds`.
- **Parks ONE unfinished thing** (F-007): the active focus task, else the top open backlog task, in the `app_state` SQLite KV (`state_store.set_app_state` / `get_app_state_entry` / `delete_app_state`, key `parked_item`). `morning_briefing` offers it back and clears it only on a successful, non-suppressed announce. Items older than 36 h are dropped, not announced as "last night".
- **Gates:** an active guided routine (the evening routine auto-triggers at 21:00) → no speech, the digest is mirrored, the item stays parked. DND → parks silently, no card.
- **Mirroring:** `jobs_calendar._mirror_message` posts the morning digest (skip path), the evening digest and the missed-reminder recap as a dashboard inbox `message` card and via `telegram_bot.fire_system_message` (no-op when the bot is off). No Web Push under DND.
- Speaker route: the `briefing` category.
- **Monitoring:** `bgw_evening_briefing_outcome_total{outcome}` (no panel or alert; `skipped_routine` is normal on routine nights). Heartbeat `bgw_evening_briefing_last_run_timestamp_seconds` → `EveningBriefingStale` (25.5 h + `for: 30m`; the threshold rationale, incl. the 25 h DST fall-back night, is in `monitoring/prometheus/alert-rules.yml`).

**Heartbeats** (morning/evening briefing, wind-down nudge + dim) go through `orchestrator/job_heartbeat.py` (docstring = contract): `stamp()` persists the run time to `app_state` (`heartbeat_<job>_ts`), `seed()` restores the real last run at startup (the first enable records the registration time once), `forget()` drops the key of a job not scheduled at startup, so re-enabling it doesn't page on an old stamp. Accepted edge: a restart across a fire time skips that run, and the Stale alert then fires truthfully.

### Sleep wind-down ladder

`orchestrator/jobs_winddown.py` (module docstring = overview). Two cron rungs ahead of `WIND_DOWN_BEDTIME` (default 22:30). Registration in `orchestrator.py` is try/except-guarded, and the minute math wraps past-midnight bedtimes.

| Rung | Does | Rules |
|------|------|-------|
| `wind_down_dim` (T-60) | Activates each scene in the comma-separated `WIND_DOWN_SCENE` via `ha_client.call_service(scene, "turn_on")`. Silent | Non-`scene.*` entries are dropped with a WARN: a `.env` typo must not turn on an arbitrary domain every night. Per-scene error isolation. No-op when unset. Skipped under DND (a scene can raise lights an early goodnight turned off) |
| `wind_down_nudge` (T-30) | Speaks a screens-away line + a one-line tomorrow anchor (`get_tomorrow_events()`; no anchor when a calendar source failed) on the `briefing` speaker route | Same DND / active-routine silent-skip rules as the evening briefing. Outcome-honest log |

**Morning half.** `sleep_mode("on")` stamps `app_state.sleep_started_at` via `tool_handlers._stamp_sleep_started` — **goodnight intent only**: indefinite mutes (no `duration_hours`) between 20:00 and 05:00. The same tool is also "mute, guests over" and "quiet for 2 h", and stamping those would falsely soften the next morning. `morning_briefing` reads + clears the stamp and goes gentle (softer greeting, weather skipped) when the night ran under `WIND_DOWN_SHORT_NIGHT_HOURS` (6.5 h). Stamps more than 16 h old are ignored, so afternoon timed mutes don't count.

**Monitoring.** Heartbeats `bgw_wind_down_last_run_timestamp_seconds` (T-30 nudge) and `bgw_wind_down_dim_last_run_timestamp_seconds` (T-60 dim), both persisted + seeded via `job_heartbeat`. The dim stamp is written at the top of `wind_down_dim`, before its no-scene / DND early returns, so it proves the job fired even on nights it does nothing (the scene counter alone can't tell a dropped job from a legitimate no-op). The nudge has the `WindDownNudgeStale` warning alert (25.5 h, quiet Pushover): a missing spoken nudge is invisible by omission, and the feature targets the person least likely to notice. The dim rung has a heartbeat panel but no alert, by design (a failed dim is visible in the house). Grafana row "Sleep Wind-Down".

Tests: `tests/test_evening_briefing.py`, `tests/test_wind_down.py`, `tests/test_morning_briefing.py` (parked-item pickup, short-night morning).

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
