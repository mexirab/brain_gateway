# F-007: Interruption Recovery

**Priority:** P1 — Should Have
**Status:** Done
**Depends on:** F-003 Task Decomposition, F-004 Focus Sessions, TTS (done)
**Blocks:** None

---

## ADHD Insight

When ADHDers get interrupted, the entire mental context can vanish. Getting back requires rebuilding everything from scratch — which often doesn't happen.

## What Jess Does

Remembers what you were doing before an interruption and helps you pick back up. Acts as external working memory.

## Interaction Examples

```
User: "Hey Jess, I need to take this call"

Jess: "Got it — bookmarking your spot. You were working on the Conjure OAuth flow.
       I'll check in when you're back."

  [20 minutes later]

Jess: "Welcome back. You were on the Conjure OAuth flow — the token refresh logic.
       Want to jump back in? I can start a focus session."
```

```
User: "Hey Jess, what was I working on?"

Jess: "Your last three things:
       1. Conjure OAuth flow — 45 minutes ago
       2. PR review for dashboard calendar — 2 hours ago
       3. Email to the landlord — this morning"
```

---

## Implementation

### New File: `orchestrator/context_tracker.py`

```python
@dataclass
class ContextBookmark:
    description: str           # what user was doing
    detail: str | None         # specific sub-task if known
    task_id: str | None        # linked decomposed task if active
    focus_session_id: str | None
    bookmarked_at: datetime
    resumed: bool

# Module state
_context_stack: deque[ContextBookmark] = deque(maxlen=10)
_interrupted: bool = False
_interrupt_bookmark: ContextBookmark | None = None

async def bookmark_context(description: str = None) -> dict
    """Explicitly bookmark current context ('I need to take this call').
    Auto-captures from active focus session or decomposed task.
    Sets _interrupted = True, schedules return check-in."""

async def auto_bookmark() -> None
    """Called when focus session interrupted or voice goes silent.
    Captures current state without explicit user command."""

async def check_in_after_interrupt() -> None
    """APScheduler job. After configurable delay, TTS:
    'Welcome back. You were working on {X}. Want to jump back in?'
    Fires once per interruption."""

async def get_recent_context(count: int = 3) -> list[dict]
    """Return last N context entries for 'what was I doing?' queries."""

async def record_context(description: str) -> None
    """Passively record context when user starts focus, begins task, etc.
    Called internally by focus_manager and task_decomposition."""
```

### Tools

```json
{
  "name": "bookmark_context",
  "description": "Bookmark current work context before an interruption. Use when user says 'I need to take a call', 'stepping away', 'be right back', etc.",
  "parameters": {
    "type": "object",
    "properties": {
      "description": { "type": "string", "description": "What user is working on (if not already known)" }
    }
  }
}
```

```json
{
  "name": "recall_context",
  "description": "Recall what user was working on. Use for 'what was I doing?', 'where was I?', 'what was I working on?'",
  "parameters": {
    "type": "object",
    "properties": {
      "count": { "type": "integer", "default": 3, "description": "Recent contexts to return" }
    }
  }
}
```

### Auto-Capture Sources

Context is passively recorded from:

| Source | What's captured | When |
|--------|----------------|------|
| `focus_manager.py` | Task description from `start_focus` | On session start |
| `task_decomposition.py` | Current step of decomposed task | On step advance |
| `routine_manager.py` | "In morning/evening routine" | On routine start |
| Explicit bookmark | User's description | On "I need to take this call" |

### Post-Interruption Flow

1. User says "I need to take this call" → `bookmark_context` fires
2. If focus session active → auto-pause (don't end it)
3. Schedule check-in for `INTERRUPT_CHECKIN_DELAY` minutes later
4. Timer fires → TTS with context + offer to resume
5. User says "resume" or starts focus → mark bookmark as resumed

### Modified Files

| File | Changes |
|------|---------|
| `tool_definitions.py` | Add `bookmark_context`, `recall_context` schemas |
| `tool_handlers.py` | Add handlers, dispatch |
| `focus_manager.py` | Call `record_context()` on session start; pause on bookmark |
| `task_decomposition.py` | Call `record_context()` on step advance |

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `INTERRUPT_CHECKIN_DELAY` | `5` | Minutes after interruption before check-in |
| `CONTEXT_STACK_SIZE` | `10` | Max rolling context entries |

---

## Testing Checklist

- [ ] Explicit bookmark captures context
- [ ] Auto-capture from active focus session
- [ ] Auto-capture from active decomposed task
- [ ] Post-interruption check-in fires after delay
- [ ] Check-in includes correct context description
- [ ] "What was I working on?" returns last 3 items with timestamps
- [ ] Focus session pauses (not ends) on interruption
- [ ] Context stack rolls over after max size
- [ ] Multiple interruptions handled correctly
