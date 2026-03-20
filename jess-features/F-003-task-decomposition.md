# F-003: Task Decomposition Engine

**Priority:** P0 — Must Have
**Status:** Done
**Depends on:** F-001 Brain Dump, RAG (done), reminders (done)
**Blocks:** F-007 Interruption Recovery, F-009 Decision Simplifier

---

## ADHD Insight

Big undefined tasks paralyze ADHD brains. Executive function struggles mean you can't plan and break things down yourself. The ambiguity alone triggers avoidance.

## What Jess Does

When you capture a task, Jess breaks it into micro-steps small enough that each one feels achievable. Each step is concrete and actionable. Jess can show "just the next step" to reduce overwhelm.

## Interaction Examples

```
User: "Hey Jess, I need to do my taxes"

Jess: "I'll break that down. Here's your first step:
       Find last year's tax return — check your email for 'TurboTax' or 'tax return'.
       Want me to search your email for it?"

User: "Done, what's next?"

Jess: "Nice. Next: gather your W-2s. Check email from your employer
       around late January. Should take about 5 minutes."
```

```
User: "Hey Jess, break down 'set up the new server'"

Jess: "Here are the steps:
       1. Unbox and rack the hardware — 15 min
       2. Connect power and ethernet — 5 min
       3. Boot and access BIOS — 10 min
       4. Install Ubuntu Server — 30 min
       5. Configure static IP — 10 min
       6. Install Docker — 10 min
       7. Pull and start services — 20 min
       Total: about 1 hour 40 minutes.
       Want me to start you on step 1?"
```

---

## Implementation

### New File: `orchestrator/task_decomposition.py`

```python
@dataclass
class MicroStep:
    index: int
    description: str          # concrete, actionable text
    est_minutes: int          # with ADHD buffer (model estimate x1.5)
    completed: bool
    skipped: bool

@dataclass
class DecomposedTask:
    task_id: str              # UUID
    original_text: str
    steps: list[MicroStep]
    current_step_index: int
    created_at: datetime
    mode: str                 # "full_list" or "next_step_only"

# Module state
_active_tasks: dict[str, DecomposedTask] = {}  # task_id → task

async def decompose_task(task_text: str, context: str = "") -> DecomposedTask
    """Use unified model to break task into micro-steps.
    - Each step must be a single concrete action (verb + object)
    - Estimate time per step (apply 1.5x ADHD buffer)
    - If RAG has relevant context (e.g., past similar tasks), include it
    """

async def get_next_step(task_id: str) -> dict
    """Return only the next uncompleted step.
    'Just the next step' mode — reduces overwhelm."""

async def complete_step(task_id: str, step_index: int = None) -> dict
    """Mark current step done, return next step or completion summary."""

async def list_active_tasks() -> list[dict]
    """Return all in-progress decomposed tasks."""

async def abandon_task(task_id: str) -> dict
    """Stop tracking a task. No guilt messaging."""
```

### New Tools

```json
{
  "name": "decompose_task",
  "description": "Break a large or ambiguous task into concrete micro-steps with time estimates. Use when user mentions a task that seems big, vague, or overwhelming, or when they explicitly ask to break something down.",
  "parameters": {
    "type": "object",
    "properties": {
      "task": { "type": "string", "description": "The task to decompose" },
      "mode": { "type": "string", "enum": ["full_list", "next_step_only"], "default": "next_step_only" },
      "context": { "type": "string", "description": "Optional context about the task" }
    },
    "required": ["task"]
  }
}
```

```json
{
  "name": "task_step",
  "description": "Advance a decomposed task: complete current step, skip it, get next step, or abandon the task.",
  "parameters": {
    "type": "object",
    "properties": {
      "task_id": { "type": "string" },
      "action": { "type": "string", "enum": ["done", "skip", "next", "abandon"] }
    },
    "required": ["task_id", "action"]
  }
}
```

### Modified Files

| File | Changes |
|------|---------|
| `tool_definitions.py` | Add `decompose_task` and `task_step` schemas |
| `tool_handlers.py` | Add handlers, add to `execute_tool()`, add `decompose_task` to `TERMINAL_TOOLS` |
| `prompt_builder.py` | Add active-task context so model knows decomposed tasks exist |

### Design Decisions

- **Model does the decomposition.** The tool captures the result, not the logic. The unified model is prompted to produce step-by-step breakdowns with concrete actions and time estimates.
- **ADHD time buffer:** All model-estimated times multiplied by 1.5x before presenting. If model says 20 min, present 30.
- **"Next step only" is the default.** Full list available on request but the ADHD-optimal mode is one step at a time.
- **No persistence.** Tasks live in memory. Restart = lost. Fine for working sessions.

---

## Testing Checklist

- [ ] "Break down: do my taxes" produces 5+ concrete steps
- [ ] Each step starts with a verb and is a single action
- [ ] Time estimates include ADHD buffer
- [ ] "Next step only" mode shows only step 1
- [ ] "Done" advances to next step
- [ ] "Skip" advances without marking complete
- [ ] Completion summary at end: "All done — 6 of 7 steps completed"
- [ ] Multiple simultaneous decomposed tasks tracked independently
- [ ] "What was I working on?" returns active tasks
- [ ] "Abandon" cleans up without guilt messaging
