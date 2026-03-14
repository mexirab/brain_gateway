# F-001: Voice-First Brain Dump

**Priority:** P0 — Must Have
**Status:** Done (v1 — voice capture, auto-categorization, routing to RAG/reminders, dedup)
**Depends on:** Voice pipeline (done), RAG (done), reminders (done)
**Blocks:** F-003 Task Decomposition, F-005 Progress Tracking

---

## ADHD Insight

ADHD brains lose thoughts in seconds. Opening an app to capture them adds fatal friction — by the time you unlock, find the app, and navigate to the right screen, the thought is gone.

## What Jess Does

Captures thoughts instantly via voice and auto-categorizes them. "Hey Jess, brain dump: I need to restructure the Conjure auth flow, also buy cat food, also look into that tax thing." Jess parses this into separate items, categorizes each, and routes them to the right destination.

## Interaction Examples

```
User: "Hey Jess, brain dump: I need to call the insurance company,
       pick up my prescription, and look into refinancing options"

Jess: "Got it — three things captured:
       1. Call insurance company — added as a reminder for tomorrow morning.
       2. Pick up prescription — added to your errands list.
       3. Look into refinancing — saved as a research task.
       Anything else?"
```

```
User: "Hey Jess, remember that I like the Thai place on Westheimer,
       the one with the green curry"

Jess: "Noted — saved to your preferences."
```

---

## Implementation

### New File: `orchestrator/brain_dump_manager.py`

```python
@dataclass
class CapturedItem:
    raw_text: str
    category: str          # task, reminder, idea, errand, preference, research
    routed_to: str         # reminder, rag, clickup, shopping_list
    confidence: float
    created_at: datetime

@dataclass
class BrainDumpResult:
    items: list[CapturedItem]
    raw_input: str
    summary: str           # TTS-friendly confirmation

# Core functions
async def process_brain_dump(text: str) -> BrainDumpResult
    """Parse multi-item voice dump into categorized items.
    Uses the unified model to classify and split items.
    Routes each item to the appropriate destination."""

async def route_item(item: CapturedItem) -> str
    """Route a single item:
    - task/errand → ClickUp (future) or reminder
    - reminder → set_reminder via reminder_manager
    - idea/research → RAG via /api/memory/add
    - preference → RAG with category='preference'
    - shopping → shopping list (future, initially just RAG)
    """
```

### New Tool: `brain_dump`

```json
{
  "name": "brain_dump",
  "description": "Capture one or more thoughts, tasks, ideas, or reminders from a brain dump. Automatically categorizes and routes each item. Use when the user says 'brain dump', 'remember', 'capture', 'note to self', or lists multiple things to remember.",
  "parameters": {
    "type": "object",
    "properties": {
      "items": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "text": { "type": "string", "description": "The captured thought or task" },
            "category": { "type": "string", "enum": ["task", "reminder", "idea", "errand", "preference", "research"] },
            "urgency": { "type": "string", "enum": ["now", "today", "soon", "someday"] }
          },
          "required": ["text", "category"]
        },
        "description": "Parsed items from the brain dump"
      }
    },
    "required": ["items"]
  }
}
```

**Design note:** The model does the parsing/classification (it's already good at this). The tool handler just routes. This keeps the tool simple and leverages the model's language understanding.

### Modified Files

| File | Changes |
|------|---------|
| `tool_definitions.py` | Add `brain_dump` schema |
| `tool_handlers.py` | Add `tool_brain_dump()` handler, add to `execute_tool()`, add to `TERMINAL_TOOLS` |
| `prompt_builder.py` | Add instruction: "When user says 'brain dump' or lists multiple things, use the brain_dump tool to capture all items" |

### TTS Confirmation Templates

```
Single item:   "Got it — {category}: {summary}. {routing_confirmation}."
Multi item:    "Captured {count} things: {brief_list}. All sorted."
Preference:    "Noted — saved to your preferences."
```

### Future Enhancements (Not v1)

- ClickUp task creation (requires ClickUp API integration)
- Shopping list as a distinct data store (initially just RAG)
- "Hey Jess, what did I brain dump this week?" — query recent captures

---

## Testing Checklist

- [ ] Single item brain dump ("remember to call dentist")
- [ ] Multi-item brain dump (3+ items in one utterance)
- [ ] Items correctly categorized (task vs reminder vs idea vs preference)
- [ ] Reminders routed to `reminder_manager`
- [ ] Ideas/preferences stored in RAG with correct metadata
- [ ] TTS confirmation reads back what was captured
- [ ] Duplicate detection (don't re-store the same preference)
- [ ] Works via voice pipeline and via chat
