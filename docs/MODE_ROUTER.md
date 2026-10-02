# Mode Router (Personalized Coaching)

Deterministic v1 intent classifier. Adapts Jess's system prompt based on what Nadim needs.

## Modes

| Mode | When | Behavior |
|------|------|----------|
| explainer | Curiosity/mechanism questions, default for low intensity | Analytical, structured, no coaching language |
| mirror | "Analyze me", "reflect", "behavioral tendencies" | Pattern identification, ends with one question |
| counterbalance | Medium emotional intensity (lonely, shame, spiral) | Names distortions, small actions, no shame |
| challenge | "Hold me accountable", "push me", "no excuses" | Firm, one specific action, time-bound |
| baseline | High emotional intensity (panic, hopeless, can't breathe) | Low cognitive load, 2-3 options, grounding allowed |

**Global tone constraint:** Never default to grounding techniques unless intensity is high or explicitly requested.

**Prompt placement:** the selected mode block (`MODE_PROMPTS[mode]`) is rendered in the per-turn dynamic section of the system prompt — after `prompt_builder.DYNAMIC_CONTEXT_MARKER`, next to CURRENT DATE/TIME — never in the cache-stable static prefix. Switching modes therefore only invalidates the dynamic suffix of the llama.cpp prompt cache.

**Routing logged in `_routing`:** `intent_mode`, `intent_intensity`, `intent_tags` — visible in API response for debugging.

**Key file:** `orchestrator/mode_router.py`
