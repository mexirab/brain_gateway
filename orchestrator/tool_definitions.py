"""
Tool definitions for the Brain Gateway orchestrator.

Contains all tool JSON schemas that Nemotron and Helios use,
plus the dynamic HA tool builder.
"""

import logging
import time
from typing import Any, Dict, List

import shared
from shared import _HA_TOOL_CACHE_TTL, ha_client

logger = logging.getLogger(__name__)


def get_ha_tool_definition() -> Dict[str, Any]:
    """Build the home_assistant tool with current entity list (cached)."""
    if shared._ha_tool_cache and (time.time() - shared._ha_tool_cache_time) < _HA_TOOL_CACHE_TTL:
        return shared._ha_tool_cache

    entity_lines = []
    for domain in ["light", "switch", "fan", "climate", "cover", "scene", "lock"]:
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
            "description": "Search the user's personal knowledge base for relevant context. Use this when the user asks about personal information, projects, routines, preferences, medications, schedules, or anything that might be in their notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query to find relevant personal information"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_expert",
            "description": "Delegate to the expert model (Helios 120B) for ANY question requiring knowledge or reasoning. Use this for: general knowledge (books, movies, history, science), coding, analysis, explanations, factual questions, creative writing, or ANYTHING you're not certain about. The expert has broad knowledge - use it liberally.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question or task to delegate to the expert model",
                    },
                    "context": {"type": "string", "description": "Optional additional context to help the expert"},
                },
                "required": ["question"],
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
            "description": "Start a focus timer (Pomodoro) with Endel focus audio and distraction blocking. Announces break time via voice when timer ends. Helps with ADHD time blindness and hyperfocus protection.",
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
                            "email_poll",
                            "reminders",
                            "focus_timer",
                            "temperature",
                            "system_health",
                            "recent_errors",
                        ],
                        "description": "What to check: morning_briefing (did briefing run?), calendar_poll (last calendar check), email_poll (last email check), reminders (pending reminders), focus_timer (current/recent focus), temperature (server closet temps), system_health (overall status), recent_errors (error log)",
                    }
                },
                "required": ["query"],
            },
        },
    },
]


# Tool definition for Helios - just one simple tool
HELIOS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ask_orchestrator",
            "description": "Delegate an action to the smart home/personal assistant orchestrator. Use for: controlling devices (lights, fans, switches), searching personal notes, setting reminders, updating medications/projects, searching the web for real-world information, or checking financial status (budget, spending, XP, side quests).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Natural language command (e.g., 'turn off bedroom lights', 'what are my morning meds', 'remind me to call mom in 30 minutes', 'how is my budget looking')",
                    }
                },
                "required": ["command"],
            },
        },
    }
]


def get_orchestrator_tools() -> List[Dict[str, Any]]:
    """Get tools for Nemotron orchestrator (v6 hybrid mode).

    Excludes ask_expert since Helios handles expert queries in v6.
    """
    nemotron_tools = [t for t in STATIC_TOOLS if t["function"]["name"] != "ask_expert"]
    return [get_ha_tool_definition()] + nemotron_tools


def get_all_tools() -> List[Dict[str, Any]]:
    """Get all tools for unified mode (v7).

    Returns HA tool + all static tools, excluding ask_expert
    (the unified model IS the expert — no delegation needed).
    """
    unified_tools = [t for t in STATIC_TOOLS if t["function"]["name"] != "ask_expert"]
    return [get_ha_tool_definition()] + unified_tools
