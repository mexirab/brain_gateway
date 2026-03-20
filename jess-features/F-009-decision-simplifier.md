# F-009: Decision Simplifier

**Priority:** P2 — Nice to Have
**Status:** Done
**Depends on:** F-003 Task Decomposition, F-005 Progress Tracking, F-008 Self-Care Nudges, RAG (done), Calendar (done)
**Blocks:** None

---

## ADHD Insight

Choice paralysis is real. Too many options or ambiguous decisions and ADHD brains freeze. Reducing choices to 1-2 concrete options — or just making the call — removes executive function overhead.

## What Jess Does

When you're stuck, Jess narrows options or makes the decision. "What should I work on?" gets one answer, not a list. "I'm overwhelmed" gets triage: the single most important thing, everything else dismissed.

## Interaction Examples

```
User: "Hey Jess, what should I work on next?"

Jess: "Finish the PR review for Conjure. It's been open 2 days
       and it's a 20-minute task. Start there."
```

```
User: "Hey Jess, I'm overwhelmed"

Jess: "Okay. The only thing that matters right now is taking your meds —
       you haven't confirmed today's dose. Everything else can wait.
       Want me to start a focus session after?"
```

```
User: "What should I eat?"

Jess: "Two options: leftover Thai, or scrambled eggs.
       Both under 10 minutes. Pick one."
```

---

## Implementation

### New Tool: `decide_for_me`

```json
{
  "name": "decide_for_me",
  "description": "Help user make a decision when stuck or overwhelmed. Gathers context from calendar, tasks, self-care state, and preferences, then returns 1-2 concrete recommendations. Use for 'what should I do', 'what should I eat', 'I'm overwhelmed', or choice paralysis.",
  "parameters": {
    "type": "object",
    "properties": {
      "domain": { "type": "string", "enum": ["work", "food", "general", "overwhelm"] },
      "constraints": { "type": "string", "description": "Constraints like 'quick', 'healthy', 'under 30 minutes'" }
    },
    "required": ["domain"]
  }
}
```

### Handler: `tool_handlers.py`

```python
async def tool_decide_for_me(args: dict) -> str:
    domain = args["domain"]
    context = {}

    if domain in ("work", "general", "overwhelm"):
        context["active_tasks"] = await task_decomposition.list_active_tasks()
        context["upcoming"] = await calendar_client.list_events(days_ahead=3)
        context["selfcare"] = await selfcare_manager.get_selfcare_status()
        context["focus"] = focus_manager.get_status()

    if domain == "food":
        context["preferences"] = await rag_context("food preferences dietary")
        context["recent"] = await rag_context("recent meals")

    if domain == "overwhelm":
        context["triage"] = True

    return json.dumps(context)
```

**Design:** Tool gathers context; model synthesizes the decision. Key constraint: **1-2 options max, never a list.**

### Overwhelm Triage Priority Stack

```
1. Self-care (meds not taken?) → "Take your meds. That's it."
2. Imminent deadline (< 2 hours) → "You have {event} in {time}. Focus on that."
3. Smallest active task (< 15 min) → "Quick win: {task}. Knock it out."
4. Nothing urgent → "You're actually fine. Take a break, nothing's on fire."
```

### Prompt Injection (`prompt_builder.py`)

```
When using decide_for_me: return ONE concrete recommendation for work/overwhelm,
or TWO options max for food/general. Never present more than 2 options.
Be directive — user wants you to make the call.
For overwhelm: single most important thing, dismiss everything else.
```

### Modified Files

| File | Changes |
|------|---------|
| `tool_definitions.py` | Add `decide_for_me` schema |
| `tool_handlers.py` | Add handler, dispatch |
| `prompt_builder.py` | Add decision constraint instructions |

---

## Testing Checklist

- [ ] "What should I work on?" returns single recommendation with reasoning
- [ ] "I'm overwhelmed" returns triage (single priority, everything else dismissed)
- [ ] Overwhelm checks self-care state (meds first if not taken)
- [ ] "What should I eat?" returns exactly 2 options
- [ ] Food decisions reference RAG preferences
- [ ] Work decisions reference active tasks and calendar
- [ ] Never returns more than 2 options
- [ ] Directive tone, not wishy-washy
