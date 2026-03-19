# F-004: Body Doubling & Focus Sessions

**Priority:** P1 — Should Have
**Status:** Partially implemented (focus timer works, no check-ins or ambient audio options)
**Depends on:** Focus timer (done), TTS (done), Endel (done), APScheduler (done)
**Blocks:** F-005 Progress Tracking

---

## ADHD Insight

ADHD brains focus better with someone present — even virtually. Body doubling provides co-regulation and social accountability that helps bypass mental resistance to starting.

## What Exists Today

- `start_focus` tool: Pomodoro timer + Endel audio + Pi-hole blocking
- `stop_focus` / `focus_status` tools
- Timer expiry announces break via TTS

## What's Missing

1. **Periodic check-ins** — "How's it going? Still on the backend?"
2. **Flexible sprint lengths** based on energy
3. **End-of-session summary** — "You worked 52 minutes across 4 sprints."
4. **Drift detection** — if you went quiet or switched context
5. **Ambient audio options** — lo-fi, coffee shop, silence (currently only Endel)

---

## Implementation

### Modified File: `orchestrator/focus_manager.py`

Extend existing focus session state:

```python
@dataclass
class FocusSession:
    # ... existing fields ...
    task_description: str | None    # what you said you'd work on
    sprint_count: int               # sprints completed
    sprint_duration: int            # current sprint length in minutes
    check_in_interval: int          # minutes between check-ins
    total_focus_minutes: int        # accumulated across sprints
    last_check_in: datetime | None
    audio_source: str               # "endel", "lofi", "coffee_shop", "silence"

async def check_in() -> None:
    """Called by APScheduler during active focus session.
    TTS: 'How's it going? Still on {task_description}?'
    If no task set: 'Still in the zone? Keep going.'"""

async def end_sprint() -> dict:
    """Sprint timer fires. Announce break, wait for 'next sprint'."""

async def session_summary() -> str:
    """End-of-session: 'You worked {total} minutes across {count} sprints.'"""
```

### Enhanced Tool Schema: `start_focus`

```json
{
  "name": "start_focus",
  "description": "Start a body doubling focus session with customizable sprint lengths, check-ins, ambient audio, and site blocking.",
  "parameters": {
    "type": "object",
    "properties": {
      "duration_minutes": { "type": "integer", "default": 25, "description": "Sprint length" },
      "task": { "type": "string", "description": "What user is working on (for check-ins)" },
      "blocking": { "type": "boolean", "default": true },
      "check_ins": { "type": "boolean", "default": true },
      "check_in_interval": { "type": "integer", "default": 15 },
      "audio": { "type": "string", "enum": ["endel", "lofi", "coffee_shop", "silence"], "default": "endel" },
      "sprints": { "type": "integer", "default": 4, "description": "Sprints before session ends" }
    }
  }
}
```

### New Tool: `focus_sprint`

```json
{
  "name": "focus_sprint",
  "description": "Continue to next sprint, adjust sprint length, or end session with summary.",
  "parameters": {
    "type": "object",
    "properties": {
      "action": { "type": "string", "enum": ["next_sprint", "extend", "end_session"] },
      "duration_minutes": { "type": "integer", "description": "Override sprint length for next sprint" }
    },
    "required": ["action"]
  }
}
```

### Audio Sources

| Source | Implementation |
|--------|---------------|
| `endel` | Existing Endel Pacific HLS stream to office speaker |
| `lofi` | Stream URL to HA `media_player.play_media` |
| `coffee_shop` | Stream URL or local audio file |
| `silence` | No audio, just timer + check-ins |

Stream URLs configured via env vars: `FOCUS_AUDIO_LOFI_URL`, `FOCUS_AUDIO_COFFEE_URL`.

### TTS Templates

```
Session start:  "Focus session started. Working on {task} — {sprints} sprints
                 of {duration} minutes. I'll check in every {interval} minutes."
Check-in:       "How's it going? Still on {task}?"
Sprint end:     "Sprint {n} done. Take a {break} minute break.
                 Say 'next sprint' when ready."
Session end:    "Session complete. {total} minutes across {count} sprints. {encouragement}"
Encouragement:  "That's solid." / "Your best session this week." / "Nice work."
```

### Modified Files

| File | Changes |
|------|---------|
| `focus_manager.py` | Extend dataclass, add check-in/sprint/summary functions |
| `tool_definitions.py` | Update `start_focus` schema, add `focus_sprint` |
| `tool_handlers.py` | Add `tool_focus_sprint()`, update `tool_start_focus()` |
| `shared.py` | Add audio URL env vars |

---

## Testing Checklist

- [ ] Start focus with task description — check-ins reference the task
- [ ] Check-ins fire at configured interval
- [ ] Sprint end announces break and waits for "next sprint"
- [ ] End-of-session summary totals all sprints
- [ ] Different audio sources work (endel, silence, stream URLs)
- [ ] "Extend" keeps current sprint going
- [ ] "End session" generates summary at any point
- [ ] Focus blocking still works with enhanced session
- [ ] Backward compatible — existing `start_focus` calls still work
