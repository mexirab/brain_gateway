"""
Tool definitions for the Brain Gateway orchestrator.

Contains all tool JSON schemas for the unified model,
plus the dynamic HA tool builder.
"""

import logging
import time
from typing import Any, Dict, List

from orchestrator import shared
from orchestrator.shared import _HA_TOOL_CACHE_TTL, ha_client

logger = logging.getLogger(__name__)


def get_ha_tool_definition() -> Dict[str, Any]:
    """Build the home_assistant tool with current entity list (cached)."""
    if shared._ha_tool_cache and (time.time() - shared._ha_tool_cache_time) < _HA_TOOL_CACHE_TTL:
        return shared._ha_tool_cache

    entity_lines = []
    for domain in ["light", "switch", "fan", "climate", "cover", "scene", "lock", "media_player"]:
        entities = ha_client.get_entities_by_domain(domain)
        for e in entities:
            entity_lines.append(f"  - {e.entity_id} ({e.friendly_name})")

    entity_list = "\n".join(entity_lines[:60]) if entity_lines else "  (entities loading...)"

    shared._ha_tool_cache = {
        "type": "function",
        "function": {
            "name": "home_assistant",
            "description": f"""Control smart home via Home Assistant API. Call this ONCE per entity.

ENTITIES:
{entity_list}

SERVICES:
- light: turn_on (brightness 0-255, rgb_color [R,G,B]), turn_off, toggle
- switch/fan: turn_on, turn_off, toggle
- climate: set_temperature (temperature: int)
- cover: open_cover, close_cover
- scene: turn_on
- media_player: play_media (media_content_id: URL, media_content_type: "music"), media_pause, media_stop, volume_set (volume_level: 0.0-1.0)

COLORS: rgb_color as [R,G,B]. Blue=[0,0,255], Red=[255,0,0], Green=[0,255,0], Purple=[128,0,128], Yellow=[255,255,0], Orange=[255,165,0], Pink=[255,192,203], White=[255,255,255]
BRIGHTNESS: 0-255 scale. 50%=128, 75%=191, 100%=255""",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity ID like 'light.bedroom_fan_lights'"},
                    "service": {
                        "type": "string",
                        "description": "Service: turn_on, turn_off, toggle, set_temperature, etc.",
                    },
                    "data": {
                        "type": "object",
                        "description": 'Service data, e.g., {"brightness": 128, "rgb_color": [0,0,255]}',
                    },
                },
                "required": ["entity_id", "service"],
            },
        },
    }
    shared._ha_tool_cache_time = time.time()
    return shared._ha_tool_cache


# Static tools (non-HA)
STATIC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search the user's personal knowledge base (memory palace) for relevant context. Memories are organized into wings (personal, brain_gateway, conjure, infrastructure, jess) and rooms (health, routines, architecture, debugging, etc.). Use wing/room to narrow searches to a specific domain.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query to find relevant personal information"},
                    "wing": {
                        "type": "string",
                        "enum": ["personal", "brain_gateway", "conjure", "infrastructure", "jess"],
                        "description": "Optional: narrow search to a specific domain/wing",
                    },
                    "room": {
                        "type": "string",
                        "description": "Optional: narrow search to a specific room within the wing (e.g., 'health', 'architecture', 'debugging')",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": "Update or correct information in memory. Use when the user says something you know is different from what you have stored, or when explicitly asked to remember/update something. Searches for outdated facts and replaces them with the correction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "correction": {
                        "type": "string",
                        "description": "The correct/updated information to store",
                    },
                    "search_query": {
                        "type": "string",
                        "description": "What to search for to find the outdated fact(s) to replace",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["identity", "preference", "health", "routine", "project", "technical", "general"],
                        "description": "Category of the fact being corrected",
                    },
                },
                "required": ["correction", "search_query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_data",
            "description": "Update the user's structured personal data (medications, projects). Use this when they ask to add, remove, or modify medications or project information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "add_medication",
                            "remove_medication",
                            "update_medication",
                            "add_project",
                            "update_project_status",
                            "add_project_step",
                            "complete_step",
                        ],
                        "description": "The action to perform",
                    },
                    "name": {"type": "string", "description": "Medication name or project name"},
                    "dose": {"type": "string", "description": "Medication dose (e.g., '50mg')"},
                    "schedule": {
                        "type": "string",
                        "enum": ["morning", "evening", "weekly", "as_needed"],
                        "description": "When to take the medication",
                    },
                    "purpose": {"type": "string", "description": "What the medication is for"},
                    "notes": {"type": "string", "description": "Additional notes"},
                    "status": {
                        "type": "string",
                        "enum": ["not_started", "in_progress", "blocked", "done"],
                        "description": "Project status",
                    },
                    "step": {"type": "string", "description": "A project step/task to add or complete"},
                    "goal": {"type": "string", "description": "Project goal description"},
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "Project priority",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["active", "someday_maybe", "parking_lot"],
                        "description": "Project category",
                    },
                    "completed": {"type": "boolean", "description": "Whether to add step as already completed"},
                },
                "required": ["action", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a reminder for the user. The reminder will be announced via voice on home speakers and/or sent as a mobile notification at the specified time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_text": {
                        "type": "string",
                        "description": "What to remind about (e.g., 'take your Vyvanse', 'call mom', 'check the laundry')",
                    },
                    "time": {
                        "type": "string",
                        "description": "When to remind. Accepts: 'in 5 minutes', 'in 2 hours', 'at 3pm', 'at 14:30'",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["voice", "phone", "both"],
                        "description": "Where to deliver the reminder: voice=all speakers, phone=mobile notification, both=all (default: both)",
                    },
                },
                "required": ["reminder_text", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reminder",
            "description": "Cancel a pending reminder by its ID. Use list_reminders to see pending reminders first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "string",
                        "description": "The ID of the reminder to cancel (e.g., 'abc12345')",
                    }
                },
                "required": ["reminder_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_focus",
            "description": "Start a body doubling focus session with Pomodoro timer, ambient audio, check-ins, and distraction blocking. Supports multi-sprint sessions. Announces break time via voice when sprint ends.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "What you're focusing on (e.g., 'writing report', 'coding', 'emails')",
                    },
                    "duration": {"type": "integer", "description": "Focus duration in minutes (default 25)"},
                    "break_duration": {"type": "integer", "description": "Break duration in minutes (default 5)"},
                    "speaker": {
                        "type": "string",
                        "description": "Media player for audio (e.g., 'office speaker', 'bedroom', 'kitchen'). Uses default if not specified.",
                    },
                    "soundscape": {
                        "type": "string",
                        "enum": ["focus", "deeper-focus", "study", "colored-noises", "none"],
                        "description": "Endel soundscape to play (default: focus, 'none' to disable)",
                    },
                    "block_sites": {
                        "type": "boolean",
                        "description": "ALWAYS true unless user explicitly says 'without blocking' or 'no blocking'. Do not set to false unless explicitly requested.",
                    },
                    "check_ins": {
                        "type": "boolean",
                        "description": "Enable periodic check-ins during the session (default: true)",
                    },
                    "check_in_interval": {
                        "type": "integer",
                        "description": "Minutes between check-ins (default: 15). Only used when check_ins is true.",
                    },
                    "audio": {
                        "type": "string",
                        "enum": ["endel", "lofi", "coffee_shop", "silence"],
                        "description": "Ambient audio source. endel=Endel focus sounds (default), lofi=lo-fi stream, coffee_shop=ambient cafe, silence=no audio",
                    },
                    "sprints": {
                        "type": "integer",
                        "description": "Number of sprints to plan (default: 1). Set 2-6 for a multi-sprint body doubling session.",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_focus",
            "description": "Stop the current focus timer early.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_status",
            "description": "Check how much time is left in the current focus session.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "focus_sprint",
            "description": "Manage sprint transitions in an active multi-sprint focus session. Use 'next_sprint' when the user says 'next sprint' or 'start next sprint'. Use 'extend' when they want more time on the current sprint. Use 'end_session' when they want to finish and see their summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["next_sprint", "extend", "end_session"],
                        "description": "next_sprint=start next sprint, extend=add time to current sprint, end_session=finish and announce total summary",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Override sprint length for next sprint, or minutes to add for extend",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for real-world information. Use for: current events, news, weather, restaurants, sports, businesses, or any factual question about the real world that isn't in the user's personal notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g., 'Houston events this weekend', 'weather in Houston today')",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["general", "news"],
                        "description": "Search category (default: general)",
                    },
                    "time_range": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                        "description": "Optional time filter for recent results",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_calendar",
            "description": "Check the user's Google Calendar for upcoming events. Use when they ask about their schedule, what's on their calendar, or what's happening today/tomorrow/this week.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": "Number of days to look ahead (default: 7). Use 1 for today, 2 for tomorrow, 7 for this week.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create a new event on the user's Google Calendar. Use when they ask to add, schedule, or create a calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Event title (e.g., 'Pickleball at Honcho')"},
                    "start_time": {
                        "type": "string",
                        "description": "Event start time in ISO 8601 format (e.g., '2026-02-21T19:00:00')",
                    },
                    "duration_minutes": {"type": "integer", "description": "Duration in minutes (default: 60)"},
                    "description": {"type": "string", "description": "Optional event description"},
                    "location": {"type": "string", "description": "Optional event location"},
                },
                "required": ["title", "start_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_email",
            "description": "Check the user's Gmail inbox for recent or unread emails. Use when they ask about their email, new messages, or want to see what's in their inbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional Gmail search query (e.g., 'from:amazon', 'subject:flight', 'is:unread')",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of emails to return (default: 10)",
                    },
                    "unread_only": {"type": "boolean", "description": "Only show unread messages (default: false)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_email",
            "description": "Search the user's Gmail with specific criteria. Use Gmail search syntax: from:, to:, subject:, has:attachment, after:YYYY/MM/DD, before:YYYY/MM/DD, newer_than:2d, older_than:1w.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Gmail search query (e.g., 'from:bank after:2026/02/01', 'has:attachment subject:invoice', 'from:amazon newer_than:7d')",
                    },
                    "max_results": {"type": "integer", "description": "Maximum number of results (default: 10)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finance_status",
            "description": "Check the user's Financial Quest Board status: budget remaining, XP/level, streak, side quests, and spending summary. Use when they ask about their finances, budget, spending, how much they have left, savings goals, or game progress.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_details": {
                        "type": "boolean",
                        "description": "If true, include detailed breakdown (investing, retirement, side quests). Default false for quick summary.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
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
                                "text": {"type": "string", "description": "The captured thought or task"},
                                "category": {
                                    "type": "string",
                                    "enum": ["task", "reminder", "idea", "errand", "preference", "research"],
                                    "description": "Category of the item",
                                },
                                "urgency": {
                                    "type": "string",
                                    "enum": ["now", "today", "soon", "someday"],
                                    "description": "How urgent the item is",
                                },
                            },
                            "required": ["text", "category"],
                        },
                        "description": "Parsed items from the brain dump",
                    }
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decompose_task",
            "description": "Break a large or ambiguous task into concrete micro-steps with time estimates. Use when user mentions a task that seems big, vague, or overwhelming, or when they explicitly ask to break something down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "The task to decompose"},
                    "mode": {
                        "type": "string",
                        "enum": ["full_list", "next_step_only"],
                        "description": "full_list shows all steps, next_step_only shows one at a time (default: next_step_only)",
                    },
                    "context": {"type": "string", "description": "Optional context about the task"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_step",
            "description": "Advance a decomposed task: complete current step, skip it, get next step, list active tasks, or abandon the task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The task ID (e.g., 'abc12345')"},
                    "action": {
                        "type": "string",
                        "enum": ["done", "skip", "next", "list", "abandon"],
                        "description": "done=mark step complete, skip=skip step, next=show next step, list=show all active tasks, abandon=stop tracking",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_routine",
            "description": "Start a morning or evening routine — guides the user step by step via voice.",
            "parameters": {
                "type": "object",
                "properties": {
                    "routine_id": {
                        "type": "string",
                        "enum": ["morning", "evening"],
                        "description": "Which routine to start",
                    }
                },
                "required": ["routine_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_action",
            "description": "Advance the active routine: mark step done, skip it, pause/resume, stop early, or check status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["done", "skip", "pause", "resume", "stop", "status"],
                        "description": "done/skip=advance, pause/resume=hold nudges, stop=end early, status=repeat current step",
                    }
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "routine_status",
            "description": "Check the status of the active routine session.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decide_for_me",
            "description": "Help user make a decision when stuck or overwhelmed. Gathers context from calendar, tasks, self-care state, and preferences, then returns 1-2 concrete recommendations. Use for 'what should I do', 'what should I work on', 'what should I eat', 'I'm overwhelmed', 'I can't decide', or choice paralysis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "enum": ["work", "food", "general", "overwhelm"],
                        "description": "Decision domain: work (what to work on), food (what to eat), general (anything else), overwhelm (triage mode)",
                    },
                    "constraints": {
                        "type": "string",
                        "description": "Optional constraints like 'quick', 'healthy', 'under 30 minutes'",
                    },
                },
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "selfcare_log",
            "description": "Log a self-care action OR check current status. ALWAYS call this when user mentions eating (had lunch, grabbed a snack, just ate), taking medication (took my meds, yes I took it), drinking water, or exercising. Use action='check' when user asks 'did I take my meds?', 'what have I logged today?', 'have I eaten?'. Must be logged even if you also respond conversationally.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["meal", "medication", "water", "movement", "check"],
                        "description": "Type of self-care action. Use 'check' to query what's been logged today without logging anything new.",
                    },
                    "detail": {
                        "type": "string",
                        "description": "Medication name or meal type (optional)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bookmark_context",
            "description": "Bookmark current work context before an interruption. Use when user says 'I need to take a call', 'stepping away', 'be right back', 'brb', 'I need to handle something'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What the user is working on (auto-detected from active focus/task if omitted)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_context",
            "description": "Recall what user was working on recently. Use when they ask 'what was I doing?', 'where was I?', 'what was I working on?', 'I just got back'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent contexts to return (default: 3)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_system",
            "description": "Check Brain Gateway system status, recent activity, and logs. Use when the user asks about system behavior like 'did my morning briefing run?', 'are my reminders working?', 'what happened with the calendar?', 'any temperature alerts?', 'is everything running?', 'any errors?'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "enum": [
                            "morning_briefing",
                            "calendar_poll",
                            "reminders",
                            "focus_timer",
                            "temperature",
                            "system_health",
                            "recent_errors",
                        ],
                        "description": "What to check: morning_briefing (did briefing run?), calendar_poll (last calendar check), reminders (pending reminders), focus_timer (current/recent focus), temperature (server closet temps), system_health (overall status), recent_errors (error log)",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Re-analyze or ask a follow-up question about the most recently shared image. Use when the user asks a follow-up question about a photo they already sent (e.g., 'what brand is that cereal?' after sending a pantry photo). The initial image analysis happens automatically — only call this tool for follow-up queries on the same image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Specific question or analysis to perform on the image (e.g., 'list all vegetables', 'what brand is that cereal?', 'read the text on the label')",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sleep_mode",
            "description": "Enable or disable Do Not Disturb / mute mode. Use when: goodnight/bedtime (action=on), good morning/wake up (action=off), or 'mute for X hours' / 'be quiet' / 'people are over' / 'guests' (action=on with duration_hours). When enabled, ALL speaker announcements are suppressed. Auto-unmutes after duration_hours if set.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["on", "off"],
                        "description": "on = mute all announcements, off = unmute",
                    },
                    "duration_hours": {
                        "type": "number",
                        "description": "Hours to stay muted before auto-unmuting. Omit for indefinite (until morning or manual off).",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shopping_list",
            "description": "Manage the user's shopping/grocery list. ALWAYS call this when the user says 'add X to my shopping list', 'add X to my grocery list', 'what's on my list', 'remove X from my list', or 'clear checked items'. Supports multiple named lists (grocery, shopping, hardware, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "list", "remove", "check", "uncheck", "clear_checked"],
                        "description": "Action to perform",
                    },
                    "item": {
                        "type": "string",
                        "description": "Item name (for add action)",
                    },
                    "item_id": {
                        "type": "integer",
                        "description": "Item ID (for remove/check/uncheck actions)",
                    },
                    "list_name": {
                        "type": "string",
                        "description": "List name: grocery, shopping, hardware, etc. Defaults to grocery.",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "document_vault",
            "description": "Search, browse, create, or update documents in the user's vault. Use 'create' to make a new document (food inventory, personal lists, notes). Use 'search' to find documents, 'list' to browse by category, 'update' to add/replace notes on a document. When the user provides details about a document (VIN, account number, policy info), use 'update' to save those as notes. Documents are also findable via search_memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "list", "update", "create"],
                        "description": "search = find documents by query, list = browse by category, update = add/replace notes or metadata on a document, create = create a new text-based document (food inventory, lists, notes)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query (for search action)",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["auto", "financial", "medical", "legal", "insurance", "personal", "housing", "other"],
                        "description": "Category filter (for list action)",
                    },
                    "doc_id": {
                        "type": "string",
                        "description": "Document ID (for update action — get this from search/list results)",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Notes to save on the document (for update action). Will replace existing notes.",
                    },
                    "title": {
                        "type": "string",
                        "description": "New title for the document (for update action, optional)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_claude_activity",
            "description": "Check what Claude Code (the CLI coding assistant) has been working on recently. Use this when the user asks you to troubleshoot something that may have just changed, says 'what did I just do with Claude Code', mentions code they were editing, or asks you to help diagnose something that might be related to recent changes. Returns recent turns, files touched, or the current live session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["recent", "current_session", "files_touched"],
                        "description": "recent = last N turns from the buffer (Stop hook required for freshest data); current_session = read live .jsonl session file directly; files_touched = list of files Claude Code has edited",
                    },
                    "minutes_back": {
                        "type": "integer",
                        "description": "How far back to look in minutes (default: 120)",
                    },
                },
                "required": ["action"],
            },
        },
    },
]

# Code agent tool — added dynamically when enabled
_CODE_AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "code_agent",
        "description": (
            "Delegate a coding task to the code agent: troubleshoot issues, read source code, "
            "search for patterns, run tests, or make changes to the Brain Gateway codebase. "
            "Use this when the user asks about how something works in the code, asks you to "
            "fix a bug, investigate an error, or implement a feature."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Description of what to investigate or implement",
                },
                "apply_changes": {
                    "type": "boolean",
                    "description": "If true, the agent can write files. If false (default), diagnosis and investigation only.",
                    "default": False,
                },
            },
            "required": ["task"],
        },
    },
}


def get_all_tools() -> List[Dict[str, Any]]:
    """Get all tools for unified mode (v7): HA tool + all static tools + optional code agent."""
    tools = [get_ha_tool_definition()] + STATIC_TOOLS
    if shared.CODE_AGENT_ENABLED:
        tools.append(_CODE_AGENT_TOOL)
    return tools
