# Jess — The Home Brain: Feature Specs

Implementation specs for Claude Code. One file per feature.

**Codebase:** Brain Gateway (`/opt/jupiter/gateway_mvp/`)
**Architecture:** v7 unified mode — single model (Qwen3.5-27B on Helios) handles conversation + tools in one agentic loop via `unified_loop.py`. Falls back to Saturn (Nemotron-8B) if Helios unavailable.
**Core principle:** *If I have to open an app, I'll never go back to check it.*

## Features

| ID | Feature | Priority | Spec | Status |
|----|---------|----------|------|--------|
| F-001 | [Voice-First Brain Dump](./F-001-brain-dump.md) | P0 | Ready | Done |
| F-002 | [Proactive Time-Aware Nudges](./F-002-time-nudges.md) | P0 | Ready | Done |
| F-003 | [Task Decomposition Engine](./F-003-task-decomposition.md) | P0 | Ready | Done |
| F-004 | [Body Doubling & Focus Sessions](./F-004-body-doubling.md) | P1 | Ready | Done |
| F-005 | [Dopamine-Aware Progress Tracking](./F-005-progress-tracking.md) | P1 | Ready | Done |
| F-006 | [Context-Aware Routine Scaffolding](./F-006-routine-scaffolding.md) | P1 | Ready | Done |
| F-007 | [Interruption Recovery](./F-007-interruption-recovery.md) | P1 | Ready | Not started |
| F-008 | [Meal & Self-Care Nudges](./F-008-selfcare-nudges.md) | P2 | Ready | Not started |
| F-009 | [Decision Simplifier](./F-009-decision-simplifier.md) | P2 | Ready | Not started |
| F-010 | [Ambient Awareness Mode](./F-010-ambient-awareness.md) | P2 | Ready | Partial — dashboard exists |

## Build Order

### Phase 1 — Capture & Time Awareness (Weeks 1-3)

| Feature | Rationale |
|---------|-----------|
| F-001 Brain Dump | Foundation — everything captured flows through here |
| F-002 Tiered Nudges | Enhancement to existing calendar polling, small diff |

### Phase 2 — Structure & Guidance (Weeks 4-7)

| Feature | Rationale |
|---------|-----------|
| F-003 Task Decomposition | Makes captured tasks actionable |
| F-006 Routine Scaffolding | Daily structure — high daily-driver value |
| F-007 Interruption Recovery | Complements focus sessions and decomposed tasks |

### Phase 3 — Body & Reward (Weeks 8-10)

| Feature | Rationale |
|---------|-----------|
| F-004 Body Doubling Enhancement | Extends existing focus timer |
| F-005 Progress Tracking | Needs F-001/F-003/F-006 generating events to track |
| F-008 Self-Care Nudges | Uses med data already in YAML |

### Phase 4 — Intelligence & Ambient (Weeks 11-13)

| Feature | Rationale |
|---------|-----------|
| F-009 Decision Simplifier | Needs F-003/F-005/F-008 for full context |
| F-010 Ambient Awareness | Aggregates all other features into passive awareness |

## Dependency Graph

```
F-001 Brain Dump ──────► F-003 Task Decomposition ──► F-009 Decision Simplifier
         │                        │                            ▲
         │                        ▼                            │
         │               F-007 Interruption Recovery           │
         │                                                     │
         ▼                                                     │
F-005 Progress Tracking ◄── F-006 Routine Scaffolding         │
         ▲                         │                           │
         │                         ▼                           │
F-004 Focus Enhancement    F-008 Self-Care Nudges ─────────────┘
                                   │
                                   ▼
                           F-010 Ambient Awareness (aggregates all)
                                   ▲
                                   │
                           F-002 Tiered Nudges (standalone enhancement)
```

## Pattern for All Features

1. **New manager module:** `orchestrator/{feature}_manager.py` — state dataclass + async functions (follow `focus_manager.py`)
2. **New tools:** Schemas in `tool_definitions.py`, handlers in `tool_handlers.py`, dispatch in `execute_tool()`
3. **Background jobs:** Scheduled triggers in `background_jobs.py`, registered at startup
4. **System prompt context:** Active-state context in `prompt_builder.py`
5. **API endpoints (optional):** REST endpoints in `api_routes.py` for dashboard
6. **Dashboard widget (optional):** Card component in `frontend/src/components/dashboard/`
7. **Terminal tools:** State-changing tools added to `TERMINAL_TOOLS` set

## Global Design Rules

1. **Push, don't pull.** TTS announcements, not dashboard checks.
2. **One thing at a time.** Never present lists when a single recommendation will do.
3. **No guilt.** Skipping, missing, abandoning — Jess never makes you feel bad.
4. **Graceful degradation.** Every feature works independently.
5. **No new mic requirements.** "Hey Jess" wake word + APScheduler TTS only.
6. **In-memory by default.** Unless persistence needed for history (progress tracking → SQLite).
7. **Terminal tools.** State-changing tools go in `TERMINAL_TOOLS`.

## Existing Infrastructure

| Capability | Key File |
|------------|----------|
| TTS announcements | `tool_handlers.py` → `_announce_voice()` |
| Voice pipeline | "Hey Jess" → ATOM Echo → HA → STT → orchestrator |
| Scheduled jobs | APScheduler in `background_jobs.py` |
| Google Calendar | `google_calendar.py` |
| Gmail | `google_gmail.py` |
| Home Assistant | `ha_integration.py` |
| RAG memory | ChromaDB `nadim_rag`, 154+ docs |
| Focus timer | `focus_manager.py` |
| Reminders | `reminder_manager.py` |
| Mode router | `mode_router.py` |
| Data management | `data_manager.py` (YAML → markdown → ChromaDB) |
| Unified agentic loop | `unified_loop.py` |
| Auto-learn | `auto_learn.py` |
