"""
Tool execution handlers: dispatcher + all tool_* functions.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict

import shared
from brain_dump_manager import process_brain_dump
from data_manager import handle_update_data
from focus_manager import tool_focus_sprint, tool_focus_status, tool_start_focus, tool_stop_focus
from google_calendar import get_calendar_client
from google_gmail import get_gmail_client
from metrics import (
    CALENDAR_API_CALLS,
    CALENDAR_API_ERRORS,
    CALENDAR_API_LATENCY,
    GMAIL_API_CALLS,
    GMAIL_API_ERRORS,
    GMAIL_API_LATENCY,
    REMINDERS_DELIVERED,
    REMINDERS_SET,
    TOOL_CALL_COUNT,
    TOOL_CALL_ERRORS,
    TOOL_CALL_LATENCY,
    WEB_SEARCH_COUNT,
    WEB_SEARCH_LATENCY,
    WEB_SEARCH_RESULTS,
)
from prompt_builder import rag_context
from reminder_manager import (
    _announce_voice,
    _send_notification,
    add_reminder,
    format_time_friendly,
    get_reminder,
    list_pending_reminders,
    mark_reminder_completed,
    parse_time_expression,
    remove_reminder,
)
from shared import (
    ha_client,
    scheduler,
)
from task_decomposition import abandon_task, complete_step, decompose_task, get_next_step, list_active_tasks, skip_step
from web_search import get_search_client

logger = logging.getLogger(__name__)


async def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Execute a tool and return the result as a string."""
    TOOL_CALL_COUNT.labels(tool=tool_name).inc()
    _tool_t0 = time.time()
    logger.info(
        f"[TOOL] Executing: {tool_name} with args: {arguments}", extra={"component": "tool", "tool_name": tool_name}
    )

    try:
        if tool_name == "home_assistant":
            return await tool_home_assistant(
                arguments.get("entity_id", ""), arguments.get("service", ""), arguments.get("data", {})
            )
        elif tool_name == "search_memory":
            return tool_search_memory(arguments.get("query", ""))
        elif tool_name == "ask_expert":
            return "ask_expert is not available — the primary model handles all queries directly."
        elif tool_name == "update_data":
            return tool_update_data(arguments)
        elif tool_name == "set_reminder":
            return await tool_set_reminder(
                arguments.get("reminder_text", ""), arguments.get("time", ""), arguments.get("target", "both")
            )
        elif tool_name == "cancel_reminder":
            return await tool_cancel_reminder(arguments.get("reminder_id", ""))
        elif tool_name == "start_focus":
            return await tool_start_focus(
                arguments.get("task", "your task"),
                arguments.get("duration", 25),
                arguments.get("break_duration", 5),
                arguments.get("speaker"),
                arguments.get("soundscape", "focus"),
                arguments.get("block_sites", True),
                arguments.get("check_ins", True),
                arguments.get("check_in_interval", 15),
                arguments.get("audio"),
                arguments.get("sprints", 1),
            )
        elif tool_name == "stop_focus":
            return await tool_stop_focus()
        elif tool_name == "focus_status":
            return await tool_focus_status()
        elif tool_name == "focus_sprint":
            return await tool_focus_sprint(
                arguments.get("action", ""),
                arguments.get("duration_minutes"),
            )
        elif tool_name == "web_search":
            return await tool_web_search(
                arguments.get("query", ""), arguments.get("category", "general"), arguments.get("time_range")
            )
        elif tool_name == "check_calendar":
            return await tool_check_calendar(arguments.get("days_ahead", 7))
        elif tool_name == "create_calendar_event":
            return await tool_create_calendar_event(
                arguments.get("title", ""),
                arguments.get("start_time", ""),
                arguments.get("duration_minutes", 60),
                arguments.get("description", ""),
                arguments.get("location", ""),
            )
        elif tool_name == "check_email":
            return await tool_check_email(
                arguments.get("query", ""), arguments.get("max_results", 10), arguments.get("unread_only", False)
            )
        elif tool_name == "search_email":
            return await tool_search_email(arguments.get("query", ""), arguments.get("max_results", 10))
        elif tool_name == "finance_status":
            return await tool_finance_status(arguments.get("include_details", False))
        elif tool_name == "brain_dump":
            return await tool_brain_dump(arguments.get("items", []))
        elif tool_name == "decompose_task":
            return await tool_decompose_task(
                arguments.get("task", ""), arguments.get("mode", "next_step_only"), arguments.get("context", "")
            )
        elif tool_name == "task_step":
            return tool_task_step(arguments.get("task_id", ""), arguments.get("action", ""))
        elif tool_name == "decide_for_me":
            return await tool_decide_for_me(arguments)
        elif tool_name == "selfcare_log":
            from selfcare_manager import get_selfcare_status, log_selfcare

            if arguments.get("action") == "check":
                status = await get_selfcare_status()
                return json.dumps(status)
            return await log_selfcare(arguments.get("action", ""), arguments.get("detail"))
        elif tool_name == "bookmark_context":
            from context_tracker import bookmark_context

            result = await bookmark_context(arguments.get("description"))
            desc = result["description"]
            delay = result["checkin_delay"]
            return (
                f"Got it — bookmarking your spot. You were working on {desc}. "
                f"I'll check in with you in {delay} minutes."
            )
        elif tool_name == "recall_context":
            from context_tracker import get_recent_context

            entries = await get_recent_context(arguments.get("count", 3))
            if not entries:
                return "I don't have any recent context recorded yet."
            lines = ["Here's what you were working on:"]
            for i, e in enumerate(entries, 1):
                lines.append(f"{i}. {e['description']} — {e['when']}")
            return "\n".join(lines)
        elif tool_name == "start_routine":
            from routine_manager import start_routine

            return await start_routine(arguments.get("routine_id", ""))
        elif tool_name == "routine_action":
            from routine_manager import advance_step

            return await advance_step(arguments.get("action", "done"))
        elif tool_name == "routine_status":
            from routine_manager import get_routine_status

            return await get_routine_status()
        elif tool_name == "check_system":
            from system_diagnostics import check_system

            return await check_system(arguments.get("query", "system_health"))
        elif tool_name == "analyze_image":
            return await tool_analyze_image(arguments.get("query", "Describe this image in detail."))
        elif tool_name == "sleep_mode":
            action = arguments.get("action", "on")
            shared.DND_ACTIVE = action == "on"
            # Persist so DND survives restarts
            import state_store

            if shared.DND_ACTIVE:
                state_store.set_notification_flag("dnd_active")
                logger.info("[DND] Sleep mode enabled — all announcements suppressed")
                return "Sleep mode on. No more announcements tonight. Good night!"
            else:
                state_store.clear_notification_flag("dnd_active")
                logger.info("[DND] Sleep mode disabled — announcements resumed")
                return "Good morning! Announcements are back on."
        elif tool_name == "shopping_list":
            import asyncio

            return await asyncio.to_thread(_handle_shopping_list, arguments)
        elif tool_name == "document_vault":
            import asyncio

            return await asyncio.to_thread(_handle_document_vault, arguments)
        else:
            return f"Unknown tool: {tool_name}"
    except Exception as e:
        TOOL_CALL_ERRORS.labels(tool=tool_name).inc()
        logger.error(
            f"[TOOL] Error executing {tool_name}: {e}",
            extra={"component": "tool", "tool_name": tool_name, "error_type": type(e).__name__},
        )
        return f"Error executing {tool_name}: {str(e)}"
    finally:
        TOOL_CALL_LATENCY.labels(tool=tool_name).observe(time.time() - _tool_t0)


def _handle_shopping_list(arguments: Dict[str, Any]) -> str:
    """Handle shopping/grocery list tool calls."""
    from state_store import (
        add_shopping_item,
        check_shopping_item,
        clear_checked_items,
        get_shopping_list,
        remove_shopping_item,
    )

    action = arguments.get("action", "list")
    list_name = arguments.get("list_name", "grocery").lower().strip()

    if action == "add":
        item = arguments.get("item", "").strip()
        if not item:
            return "No item specified."
        result = add_shopping_item(item, list_name)
        return f"Added '{result['item']}' to your {list_name} list."

    elif action == "list":
        items = get_shopping_list(list_name=list_name if list_name != "all" else None)
        if not items:
            return f"Your {list_name} list is empty."
        lines = [f"Your {list_name} list ({len(items)} items):"]
        for it in items:
            lines.append(f"  - {it['item']} (id: {it['id']})")
        return "\n".join(lines)

    elif action in ("check", "uncheck", "remove"):
        item_id = arguments.get("item_id")
        if not item_id:
            return "No item_id specified."
        iid = int(item_id)
        if action == "check":
            ok = check_shopping_item(iid, checked=True)
            return "Checked off." if ok else "Item not found."
        elif action == "uncheck":
            ok = check_shopping_item(iid, checked=False)
            return "Unchecked." if ok else "Item not found."
        else:
            ok = remove_shopping_item(iid)
            return "Removed." if ok else "Item not found."

    elif action == "clear_checked":
        count = clear_checked_items(list_name if list_name != "all" else None)
        return f"Cleared {count} checked item(s)."

    return f"Unknown action: {action}"


async def tool_home_assistant(entity_id: str, service: str, data: Dict[str, Any] = None) -> str:
    """Execute a Home Assistant service call directly."""
    if not entity_id or not service:
        return "Missing entity_id or service"

    logger.info(
        f"[HA] Calling {service} on {entity_id} with data: {data}", extra={"component": "ha", "entity_id": entity_id}
    )
    result = await ha_client.call_service(entity_id, service, data or {})

    if result.success:
        logger.info(f"[HA] Success: {result.message}")
        return result.message
    else:
        logger.warning(f"[HA] Failed: {result.message}")
        return f"Failed: {result.message}"


def tool_search_memory(query: str) -> str:
    """Search the personal knowledge base (RAG)."""
    if not query:
        return "No query provided"

    logger.info(f"[MEMORY] Searching for: {query}")
    context = rag_context(query)

    if context:
        return f"Found relevant information:\n{context}"
    else:
        return "No relevant information found in memory."


async def tool_web_search(query: str, category: str = "general", time_range: str = None) -> str:
    """Search the web via SearXNG and return formatted results."""
    if not query:
        return "No search query provided"

    WEB_SEARCH_COUNT.inc()
    _ws_t0 = time.time()
    logger.info(
        f"[WEB_SEARCH] Searching: '{query}' (category={category}, time_range={time_range})",
        extra={"component": "web_search"},
    )
    client = get_search_client(http_client=shared._http)
    response = await client.search(query=query, category=category, time_range=time_range)
    WEB_SEARCH_LATENCY.observe(time.time() - _ws_t0)

    if not response.success:
        logger.error(f"[WEB_SEARCH] Failed for '{query}': {response.error}")
        return f"Web search failed: {response.error}"

    if not response.results:
        logger.warning(f"[WEB_SEARCH] No results for '{query}'")
        return f"No results found for '{query}'"

    lines = [f"Web search results for '{query}':"]
    for i, r in enumerate(response.results, 1):
        lines.append(f"\n{i}. {r.title}")
        if r.content:
            lines.append(f"   {r.content}")
        lines.append(f"   URL: {r.url}")

    WEB_SEARCH_RESULTS.observe(len(response.results))
    logger.info(
        f"[WEB_SEARCH] Returning {len(response.results)} results for '{query}'",
        extra={"component": "web_search", "result_count": len(response.results)},
    )
    return "\n".join(lines)


async def tool_check_calendar(days_ahead: int = 7) -> str:
    """Check Google Calendar for upcoming events."""
    client = get_calendar_client(http_client=shared._http)
    if not client.is_configured:
        return "Google Calendar is not configured. Run google_setup.py first to set up OAuth2 credentials."

    CALENDAR_API_CALLS.labels(operation="list_events").inc()
    _cal_t0 = time.time()
    logger.info(f"[CALENDAR] Checking calendar for next {days_ahead} days", extra={"component": "calendar"})
    response = await client.list_events(days_ahead=days_ahead)
    CALENDAR_API_LATENCY.labels(operation="list_events").observe(time.time() - _cal_t0)

    if not response.success:
        CALENDAR_API_ERRORS.labels(operation="list_events").inc()
        return f"Calendar error: {response.error}"

    if not response.events:
        if days_ahead == 1:
            return "No events on the calendar for today."
        return f"No events on the calendar for the next {days_ahead} days."

    lines = []
    if days_ahead == 1:
        lines.append(f"Today's calendar ({len(response.events)} events):")
    else:
        lines.append(f"Calendar for the next {days_ahead} days ({len(response.events)} events):")

    for event in response.events:
        if event.all_day:
            date_str = event.start.strftime("%A %b %d")
            lines.append(f"\n- {event.title} (all day, {date_str})")
        else:
            time_str = event.start.strftime("%A %b %d, %I:%M %p")
            end_str = event.end.strftime("%I:%M %p")
            lines.append(f"\n- {event.title} — {time_str} to {end_str}")
        if event.location:
            lines.append(f"  Location: {event.location}")

    return "\n".join(lines)


async def tool_create_calendar_event(
    title: str, start_time: str, duration_minutes: int = 60, description: str = "", location: str = ""
) -> str:
    """Create a new Google Calendar event."""
    if not title:
        return "Missing event title."
    if not start_time:
        return "Missing event start time. Provide an ISO 8601 datetime like '2026-02-21T19:00:00'."

    client = get_calendar_client(http_client=shared._http)
    if not client.is_configured:
        return "Google Calendar is not configured. Run google_setup.py first to set up OAuth2 credentials."

    CALENDAR_API_CALLS.labels(operation="create_event").inc()
    _cal_t0 = time.time()
    logger.info(f"[CALENDAR] Creating event: {title} at {start_time}", extra={"component": "calendar"})
    response = await client.create_event(
        title=title,
        start_time=start_time,
        duration_minutes=duration_minutes,
        description=description,
        location=location,
    )

    CALENDAR_API_LATENCY.labels(operation="create_event").observe(time.time() - _cal_t0)
    if not response.success:
        CALENDAR_API_ERRORS.labels(operation="create_event").inc()
        return f"Failed to create event: {response.error}"

    event = response.events[0]
    time_str = event.start.strftime("%A %b %d, %I:%M %p")
    result = f"Created event: {event.title} on {time_str}"
    if location:
        result += f" at {location}"
    return result


async def tool_check_email(query: str = "", max_results: int = 10, unread_only: bool = False) -> str:
    """Check Gmail inbox for recent or unread emails."""
    client = get_gmail_client(http_client=shared._http)
    if not client.is_configured:
        return "Gmail is not configured. Run google_setup.py first to set up OAuth2 credentials."

    GMAIL_API_CALLS.labels(operation="check_email").inc()
    _gmail_t0 = time.time()

    # Build query
    q_parts = []
    if unread_only:
        q_parts.append("is:unread")
    if query:
        q_parts.append(query)
    full_query = " ".join(q_parts)

    logger.info(f"[GMAIL] Checking email: query='{full_query}', max={max_results}", extra={"component": "gmail"})
    response = await client.list_messages(query=full_query, max_results=max_results)
    GMAIL_API_LATENCY.labels(operation="check_email").observe(time.time() - _gmail_t0)

    if not response.success:
        GMAIL_API_ERRORS.labels(operation="check_email").inc()
        return f"Gmail error: {response.error}"

    if not response.messages:
        if unread_only:
            return "No unread emails in your inbox."
        if query:
            return f"No emails found matching '{query}'."
        return "No recent emails in your inbox."

    unread_count = sum(1 for m in response.messages if "UNREAD" in m.labels)
    lines = [f"Found {len(response.messages)} emails ({unread_count} unread):"]

    for msg in response.messages:
        unread_marker = " [UNREAD]" if "UNREAD" in msg.labels else ""
        date_str = msg.date.strftime("%b %d, %I:%M %p")
        lines.append(f"\n- {msg.subject}{unread_marker}")
        lines.append(f"  From: {msg.sender}")
        lines.append(f"  Date: {date_str}")
        if msg.snippet:
            lines.append(f"  Preview: {msg.snippet}")

    return "\n".join(lines)


async def tool_search_email(query: str, max_results: int = 10) -> str:
    """Search Gmail with specific criteria."""
    if not query:
        return (
            "Please provide a search query. Examples: 'from:amazon', 'subject:invoice', 'has:attachment newer_than:7d'"
        )

    client = get_gmail_client(http_client=shared._http)
    if not client.is_configured:
        return "Gmail is not configured. Run google_setup.py first to set up OAuth2 credentials."

    GMAIL_API_CALLS.labels(operation="search_email").inc()
    _gmail_t0 = time.time()

    logger.info(f"[GMAIL] Searching: query='{query}', max={max_results}", extra={"component": "gmail"})
    response = await client.list_messages(query=query, max_results=max_results, label="")
    GMAIL_API_LATENCY.labels(operation="search_email").observe(time.time() - _gmail_t0)

    if not response.success:
        GMAIL_API_ERRORS.labels(operation="search_email").inc()
        return f"Gmail search error: {response.error}"

    if not response.messages:
        return f"No emails found matching '{query}'."

    lines = [f"Search results for '{query}' ({len(response.messages)} of ~{response.total_estimate} matches):"]

    for msg in response.messages:
        unread_marker = " [UNREAD]" if "UNREAD" in msg.labels else ""
        date_str = msg.date.strftime("%b %d, %I:%M %p")
        lines.append(f"\n- {msg.subject}{unread_marker}")
        lines.append(f"  From: {msg.sender}")
        lines.append(f"  Date: {date_str}")
        if msg.snippet:
            lines.append(f"  Preview: {msg.snippet}")

    return "\n".join(lines)


def tool_update_data(arguments: Dict[str, Any]) -> str:
    """Update structured personal data (medications, projects)."""
    action = arguments.get("action", "")
    name = arguments.get("name", "")

    if not action:
        return "No action specified"
    if not name:
        return "No name specified"

    logger.info(f"[DATA] Updating: action={action}, name={name}")

    return handle_update_data(
        action=action,
        name=name,
        dose=arguments.get("dose"),
        schedule=arguments.get("schedule"),
        purpose=arguments.get("purpose"),
        notes=arguments.get("notes"),
        status=arguments.get("status"),
        step=arguments.get("step"),
        goal=arguments.get("goal"),
        priority=arguments.get("priority"),
        category=arguments.get("category"),
        completed=arguments.get("completed"),
    )


async def deliver_reminder_job(reminder_id: str):
    """Called by APScheduler at the scheduled time to deliver a reminder."""
    logger.info(f"[REMINDER] Triggering: {reminder_id}")

    reminder = get_reminder(reminder_id)
    if not reminder:
        logger.warning(f"[REMINDER] {reminder_id} not found")
        return

    if reminder.get("status") != "pending":
        logger.warning(f"[REMINDER] {reminder_id} already completed, skipping")
        return

    text = reminder.get("text", "You have a reminder")
    target = reminder.get("target", "both")
    from shared import profile

    spoken_text = f"Hey {profile.user_name}! Quick reminder: {text}"

    voice_ok = True
    if target in ["voice", "both"]:
        result = await _announce_voice(spoken_text, announcement_type="reminder")
        voice_ok = result.get("success", False)
        if not voice_ok:
            logger.error(
                f"[REMINDER] TTS FAILED for {reminder_id}: {result.get('error')}",
                extra={"component": "reminder"},
            )

    if target in ["phone", "both"]:
        await _send_notification(text)

    if voice_ok or target == "phone":
        REMINDERS_DELIVERED.inc()
        mark_reminder_completed(reminder_id)
        logger.info(f"[REMINDER] Completed: {reminder_id}", extra={"component": "reminder"})
    else:
        # Schedule a single retry in 2 minutes — but only once (check for existing retry job)
        retry_job_id = f"reminder_{reminder_id}_retry"
        existing_retry = scheduler.get_job(retry_job_id)
        if existing_retry:
            logger.error(
                f"[REMINDER] {reminder_id} TTS failed again on retry — giving up, sending phone fallback",
                extra={"component": "reminder"},
            )
            await _send_notification(text)
            REMINDERS_DELIVERED.inc()
            mark_reminder_completed(reminder_id)
        else:
            retry_time = datetime.now(shared.TIMEZONE) + timedelta(minutes=2)
            try:
                scheduler.add_job(
                    deliver_reminder_job,
                    trigger="date",
                    run_date=retry_time,
                    args=[reminder_id],
                    id=retry_job_id,
                    replace_existing=True,
                )
                logger.warning(
                    f"[REMINDER] {reminder_id} TTS failed — scheduled retry at {retry_time:%H:%M}",
                    extra={"component": "reminder"},
                )
            except Exception as retry_err:
                logger.error(
                    f"[REMINDER] {reminder_id} TTS failed and retry scheduling failed: {retry_err}",
                    extra={"component": "reminder"},
                )


async def tool_set_reminder(reminder_text: str, time_str: str, target: str = "both") -> str:
    """Set a reminder that will be delivered via voice and/or mobile notification."""
    if not reminder_text:
        return "Please tell me what to remind you about."
    if not time_str:
        return "Please tell me when to remind you. Try 'in 5 minutes' or 'at 3pm'."

    if target not in ["voice", "phone", "both"]:
        target = "both"

    logger.info(f"[REMINDER] Setting reminder: '{reminder_text}' at '{time_str}' via {target}")

    trigger_time, error = parse_time_expression(time_str)
    if error:
        return error

    # Deduplication
    DEDUP_WINDOW_SECONDS = 60
    now = datetime.now()
    for existing in list_pending_reminders():
        if existing.get("text", "").lower().strip() == reminder_text.lower().strip():
            try:
                created = datetime.fromisoformat(existing.get("created_at", ""))
                if (now - created).total_seconds() < DEDUP_WINDOW_SECONDS:
                    logger.warning(f"[REMINDER] Duplicate rejected: '{reminder_text}' (existing {existing.get('id')})")
                    return f"You already have a reminder for '{reminder_text}' - I won't create a duplicate."
            except (ValueError, TypeError):
                pass

    reminder_id = str(uuid.uuid4())[:8]

    add_reminder(reminder_id, reminder_text, trigger_time, target)

    scheduler.add_job(
        deliver_reminder_job,
        trigger="date",
        run_date=trigger_time,
        args=[reminder_id],
        id=f"reminder_{reminder_id}",
        replace_existing=True,
    )
    REMINDERS_SET.labels(target=target).inc()
    logger.info(f"[SCHEDULER] Scheduled job reminder_{reminder_id} for {trigger_time}", extra={"component": "reminder"})

    time_friendly = format_time_friendly(trigger_time)
    target_desc = {"voice": "on all speakers", "phone": "on your phone", "both": "on all speakers and your phone"}.get(
        target, ""
    )

    return f"Got it! I'll remind you to {reminder_text} {time_friendly} {target_desc}."


async def tool_cancel_reminder(reminder_id: str) -> str:
    """Cancel a pending reminder."""
    try:
        scheduler.remove_job(f"reminder_{reminder_id}")
        logger.info(f"[SCHEDULER] Removed job reminder_{reminder_id}")
    except Exception as e:
        logger.debug(f"[SCHEDULER] Job not found: {e}")

    if remove_reminder(reminder_id):
        return f"Reminder {reminder_id} cancelled."
    return f"Reminder {reminder_id} not found."


async def tool_finance_status(include_details: bool = False) -> str:
    """Check Financial Quest Board status: budget, XP, streak, side quests."""
    from finance_manager import (
        _ensure_budget_period,
        _get_level_info,
        _is_ynab_configured,
        get_db,
    )

    logger.info(f"[FINANCE] Checking status (details={include_details})", extra={"component": "finance"})

    try:
        with get_db() as conn:
            config = dict(conn.execute("SELECT * FROM finance_config WHERE id = 1").fetchone())
            game = dict(conn.execute("SELECT * FROM game_state WHERE id = 1").fetchone())
            ym = _ensure_budget_period(conn)
            budget = dict(conn.execute("SELECT * FROM budget_periods WHERE year_month = ?", (ym,)).fetchone())

            spent = budget["discretionary_spent"]
            limit = budget["discretionary_budget"]
            remaining = max(0, limit - spent)
            pct = (spent / limit * 100) if limit > 0 else 0

            level_info = _get_level_info(game["level"])
            xp_for_next = (game["level"] + 1) * 200
            xp_in_level = game["total_xp"] - (game["level"] * 200)

            lines = [
                f"Financial Quest Board — {ym}",
                "",
                f"Level {game['level']}: {level_info['title']} | XP: {game['total_xp']} ({xp_in_level}/{xp_for_next - game['level'] * 200} to next)",
                f"Streak: {game['streak_months']} months (best: {game['streak_best']})",
                "",
                f"Budget: ${spent:.2f} / ${limit:.2f} spent ({pct:.0f}%)",
                f"Remaining: ${remaining:.2f}",
            ]

            # Over budget warning
            if spent > limit:
                overspend = spent - limit
                years = config["retirement_target_age"] - config["current_age"]
                future_damage = overspend * ((1 + config["expected_return"]) ** years)
                lines.append(f"⚠ OVER BUDGET by ${overspend:.2f} (Future Self Damage: ${future_damage:.2f})")

            # Side quests
            quests = conn.execute(
                "SELECT name, target_amount, saved_amount FROM side_quests WHERE status = 'active'"
            ).fetchall()
            if quests:
                lines.append("")
                lines.append(f"Active Side Quests ({len(quests)}):")
                for q in quests:
                    q_pct = (q["saved_amount"] / q["target_amount"] * 100) if q["target_amount"] > 0 else 0
                    lines.append(f"  - {q['name']}: ${q['saved_amount']:.2f}/${q['target_amount']:.2f} ({q_pct:.0f}%)")

            if include_details:
                lines.append("")
                lines.append(
                    f"Monthly Plan: ${config['monthly_discretionary']:.2f} guilt-free, "
                    f"${config['monthly_investing']:.2f} investing, "
                    f"${config['monthly_buffer']:.2f} buffer"
                )
                lines.append(f"Investing this month: ${budget['investing_actual']:.2f}")
                lines.append(
                    f"Retirement: ${config['retirement_current']:,.2f} (target age {config['retirement_target_age']})"
                )

                # YNAB status
                if _is_ynab_configured():
                    sync = conn.execute("SELECT last_synced_at FROM ynab_sync_state WHERE id = 1").fetchone()
                    if sync and sync["last_synced_at"]:
                        lines.append(f"YNAB: Connected, last synced {sync['last_synced_at']}")
                    else:
                        lines.append("YNAB: Connected, not yet synced")
                else:
                    lines.append("YNAB: Not configured")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"[FINANCE] Failed to get status: {e}")
        return f"Error checking finance status: {str(e)}"


async def tool_decompose_task(task: str, mode: str = "next_step_only", context: str = "") -> str:
    """Break a task into micro-steps using the model."""
    if not task:
        return "Please tell me what task you'd like me to break down."

    if mode not in ("full_list", "next_step_only"):
        mode = "next_step_only"

    logger.info("[TASK_DECOMP] Decomposing: '%s' (mode=%s)", task[:100], mode)
    return await decompose_task(task, mode, context)


def tool_task_step(task_id: str, action: str) -> str:
    """Advance a decomposed task (done/skip/next/list/abandon)."""
    if action == "list":
        return list_active_tasks()

    if not task_id:
        return "Please provide a task ID. Say 'what am I working on' to see active tasks."

    if action == "done":
        return complete_step(task_id)
    elif action == "skip":
        return skip_step(task_id)
    elif action == "next":
        return get_next_step(task_id)
    elif action == "abandon":
        return abandon_task(task_id)
    else:
        return f"Unknown action '{action}'. Use: done, skip, next, list, or abandon."


async def tool_decide_for_me(arguments: Dict[str, Any]) -> str:
    """Gather context for decision-making. Model synthesizes the recommendation."""
    import json as _json

    domain = arguments.get("domain", "general")
    constraints = arguments.get("constraints", "")
    context: Dict[str, Any] = {"domain": domain, "constraints": constraints}

    try:
        # Active tasks
        if domain in ("work", "general", "overwhelm"):
            context["active_tasks"] = list_active_tasks()

            # Upcoming calendar events
            try:
                cal_client = get_calendar_client(http_client=shared._http)
                if cal_client and cal_client.is_configured:
                    response = await cal_client.get_upcoming(hours_ahead=48)
                    if response.success:
                        context["upcoming_events"] = [
                            {"title": e.title, "start": str(e.start), "all_day": e.all_day} for e in response.events[:5]
                        ]
            except Exception:
                pass

            # Self-care state (for overwhelm triage)
            try:
                from selfcare_manager import get_selfcare_status

                context["selfcare"] = await get_selfcare_status()
            except Exception:
                pass

            # Focus session state
            context["focus_active"] = shared.current_focus_session.get("active", False)
            if context["focus_active"]:
                context["focus_task"] = shared.current_focus_session.get("task")

        if domain == "food":
            # Search RAG for food preferences
            try:
                from prompt_builder import rag_context

                prefs = await rag_context("food preferences dietary restrictions allergies")
                if prefs:
                    context["food_preferences"] = prefs
            except Exception:
                pass

        if domain == "overwhelm":
            context["triage"] = True

    except Exception as e:
        logger.warning(f"[DECIDE] Context gathering failed: {e}")

    return _json.dumps(context, default=str)


async def tool_brain_dump(items: list) -> str:
    """Process a brain dump: categorize and route items."""
    if not items:
        return "Nothing to capture — no items provided."

    logger.info("[BRAIN_DUMP] Processing %d items", len(items))
    result = await process_brain_dump(items)
    return result.summary


async def tool_analyze_image(query: str) -> str:
    """Re-analyze the most recently shared image with a new query."""
    from vision_handler import analyze_image

    if not shared._vision_image_cache:
        return "No image available. The user needs to share an image first."

    # Use the most recently cached image
    last_key = list(shared._vision_image_cache.keys())[-1]
    image_data = shared._vision_image_cache[last_key]

    logger.info("[VISION_TOOL] Re-analyzing cached image with query: %s", query[:100])
    return await analyze_image(image_data, query)
def _handle_document_vault(arguments: Dict[str, Any]) -> str:
    """Handle document_vault tool calls."""
    from state_store import get_document, list_documents, save_document, update_document

    action = arguments.get("action", "list")

    if action == "search":
        query = arguments.get("query", "")
        if not query:
            return "No search query provided."
        docs = list_documents(search=query, limit=10)
        if not docs:
            return f"No documents found matching '{query}'."
        lines = [f"Found {len(docs)} document(s):"]
        for d in docs:
            size_kb = d["file_size"] // 1024
            lines.append(
                f"- **{d['title']}** (id: {d['id']}, {d['category']}, {size_kb} KB, uploaded {d['uploaded_at'][:10]})"
            )
            if d.get("tags"):
                lines.append(f"  Tags: {d['tags']}")
            if d.get("notes"):
                lines.append(f"  Notes: {d['notes']}")
        return "\n".join(lines)

    elif action == "list":
        category = arguments.get("category")
        docs = list_documents(category=category, limit=20)
        if not docs:
            label = f"in category '{category}'" if category else ""
            return f"No documents found {label}."
        lines = [f"{len(docs)} document(s){f' in {category}' if category else ''}:"]
        for d in docs:
            lines.append(f"- **{d['title']}** (id: {d['id']}, {d['category']}, uploaded {d['uploaded_at'][:10]})")
            if d.get("notes"):
                lines.append(f"  Notes: {d['notes']}")
        return "\n".join(lines)

    elif action == "update":
        doc_id = arguments.get("doc_id", "")
        if not doc_id:
            # Try to find the document by searching
            return "I need a document ID to update. Use search first to find the document."

        doc = get_document(doc_id)
        if not doc:
            return f"Document not found: {doc_id}"

        updates: Dict[str, Any] = {}
        if "notes" in arguments:
            updates["notes"] = arguments["notes"]
        if "title" in arguments:
            updates["title"] = arguments["title"]
        if "category" in arguments:
            updates["category"] = arguments["category"]

        if not updates:
            return "No updates provided."

        update_document(doc_id, updates)

        # Re-index notes in RAG so they're searchable
        if "notes" in updates and updates["notes"]:
            try:
                import shared

                rag_id = doc.get("rag_doc_id") or f"vault_{doc_id}"
                notes_id = f"{rag_id}_notes"
                embedding = shared.embedding_model.encode(updates["notes"], normalize_embeddings=True).tolist()
                shared.collection.upsert(
                    documents=[updates["notes"]],
                    embeddings=[embedding],
                    metadatas=[
                        {
                            "source": "document_vault",
                            "category": doc["category"],
                            "title": doc["title"],
                            "vault_doc_id": doc_id,
                            "kind": "chunk",
                        }
                    ],
                    ids=[notes_id],
                )
            except Exception:
                pass  # RAG index failure shouldn't block the update

        updated_fields = ", ".join(updates.keys())
        return f"Updated {updated_fields} on '{doc.get('title', doc_id)}'."

    elif action == "create":
        title = arguments.get("title", "")
        if not title:
            return "A title is required to create a document."
        notes = arguments.get("notes", "")
        if not notes:
            return "Notes/content is required to create a document."
        category = arguments.get("category", "personal")
        if category not in {"auto", "financial", "medical", "legal", "insurance", "personal", "housing", "other"}:
            category = "personal"

        import uuid
        from datetime import datetime, timezone

        doc_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "id": doc_id,
            "title": title,
            "category": category,
            "tags": arguments.get("tags", ""),
            "notes": notes,
            "file_name": f"{title.lower().replace(' ', '_')}.txt",
            "file_path": "",
            "file_type": "text/plain",
            "file_size": len(notes.encode("utf-8")),
            "extracted_text": notes,
            "rag_doc_id": f"vault_{doc_id}",
            "uploaded_at": now,
            "updated_at": now,
        }
        save_document(doc)

        # Index in RAG for searchability
        try:
            import shared

            embedding = shared.embedding_model.encode(notes, normalize_embeddings=True).tolist()
            shared.collection.upsert(
                documents=[notes],
                embeddings=[embedding],
                metadatas=[
                    {
                        "source": "document_vault",
                        "category": category,
                        "title": title,
                        "vault_doc_id": doc_id,
                        "kind": "chunk",
                    }
                ],
                ids=[f"vault_{doc_id}"],
            )
        except Exception as e:
            logger.warning(f"[DOCVAULT] RAG indexing failed for new doc: {e}")

        return f"Created document \"{title}\" (id: {doc_id}, category: {category}). It's saved and searchable."


    return f"Unknown action: {action}"
