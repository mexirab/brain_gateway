"""
Tool execution handlers: dispatcher + all tool_* functions.
"""

import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict
from zoneinfo import ZoneInfo

from orchestrator import shared
from orchestrator.brain_dump_manager import process_brain_dump
from orchestrator.data_manager import handle_update_data
from orchestrator.focus_manager import tool_focus_sprint, tool_focus_status, tool_start_focus, tool_stop_focus
from orchestrator.google_calendar import get_calendar_client
from orchestrator.google_gmail import get_gmail_client
from orchestrator.metrics import (
    CALENDAR_API_CALLS,
    CALENDAR_API_ERRORS,
    CALENDAR_API_LATENCY,
    GMAIL_API_CALLS,
    GMAIL_API_ERRORS,
    GMAIL_API_LATENCY,
    REMINDERS_DELIVERED,
    REMINDERS_SET,
    WEB_SEARCH_COUNT,
    WEB_SEARCH_LATENCY,
    WEB_SEARCH_RESULTS,
)
from orchestrator.prompt_builder import rag_context
from orchestrator.reminder_manager import (
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
from orchestrator.shared import (
    ha_client,
    scheduler,
)
from orchestrator.task_decomposition import (
    abandon_task,
    complete_step,
    decompose_task,
    get_next_step,
    list_active_tasks,
    skip_step,
)
from orchestrator.web_search import get_search_client

logger = logging.getLogger(__name__)


async def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Execute a tool and return the result as a string.

    Checks the tool registry first (for tools migrated to @register_tool),
    then falls back to the legacy if-elif chain for unmigrated tools.
    """
    from orchestrator.tool_registry import execute_tool as _registry_execute
    from orchestrator.tool_registry import (
        is_registered,
    )

    # Prefer registry — migrated tools go through the decorator pipeline
    if is_registered(tool_name):
        return await _registry_execute(tool_name, arguments)

    # All tools should be registered via @register_tool — this is a safety fallback
    logger.warning("[TOOL] Unregistered tool called: %s", tool_name)
    return f"Unknown tool: {tool_name}"


def _handle_shopping_list(arguments: Dict[str, Any]) -> str:
    """Handle shopping/grocery list tool calls."""
    from orchestrator.state_store import (
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


def tool_search_memory(query: str, wing: str = "", room: str = "") -> str:
    """Search the personal knowledge base (memory palace)."""
    if not query:
        return "No query provided"

    filter_desc = f" (wing={wing}, room={room})" if wing or room else ""
    logger.info(f"[MEMORY] Searching for: {query}{filter_desc}")
    context = rag_context(query, wing=wing, room=room)

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
    """Check calendar for upcoming events.

    Mirrors the morning briefing's source priority: phone calendar sync first
    (covers Gmail + iCloud + Work — the superset Nadim actually uses), with
    Google Calendar as the fallback when the phone sync is missing or stale
    (>24h old). Previously this tool hit Google directly and silently missed
    everything that only lived on the iPhone calendars.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from orchestrator.jobs_calendar import _parse_phone_datetime

    tz = ZoneInfo(shared.TIMEZONE)
    today = datetime.now(tz).date()
    cutoff = today + timedelta(days=days_ahead)

    events: list[dict] = []
    source: str | None = None

    # Source priority mirrors morning_briefing exactly: phone sync when fresh
    # (<24h), else Google as a fallback. Phone sync is the superset (Gmail +
    # iCloud + Work) so committing to it even on an empty-window result is
    # correct — the alternative "fall through to Google if phone finds
    # nothing" can surface Google-only events the user already deleted from
    # their phone calendars.
    phone_age = time.time() - shared._phone_calendar_sync_time if shared._phone_calendar_sync_time > 0 else float("inf")
    phone_parsed_count = 0  # how many records had a valid start time
    if shared._phone_calendar_events and phone_age < 86400:
        for ev in shared._phone_calendar_events:
            try:
                start = _parse_phone_datetime(ev.get("start", ""), tz)
                phone_parsed_count += 1
                if start.tzinfo is None:
                    start = start.replace(tzinfo=tz)
                if not (today <= start.date() < cutoff):
                    continue
                events.append(
                    {
                        "title": ev.get("title", "(No title)"),
                        "start": start,
                        "all_day": ev.get("all_day", False),
                        "location": ev.get("location", ""),
                    }
                )
            except (ValueError, TypeError):
                continue
        # Defensive: if phone cache has records but NONE parsed (e.g. the
        # iPhone Shortcut is posting empty payloads — observed 2026-04-17
        # where all records had title='' and start=''), treat the sync as
        # broken and fall through to Google rather than reporting "no events"
        # from a corrupted source.
        if phone_parsed_count == 0:
            logger.warning(
                f"[CALENDAR] Phone cache has {len(shared._phone_calendar_events)} records but zero parsed — "
                f"likely broken iPhone Shortcut payload. Falling through to Google.",
                extra={"component": "calendar"},
            )
        else:
            source = "phone"
            logger.info(
                f"[CALENDAR] Using phone sync ({len(events)} events in {days_ahead}d, "
                f"{phone_parsed_count}/{len(shared._phone_calendar_events)} records parseable, "
                f"synced {int(phone_age / 60)}m ago)",
                extra={"component": "calendar"},
            )

    if source is None:
        client = get_calendar_client(http_client=shared._http)
        if not client.is_configured:
            return "Calendar is not available — phone sync has no recent data and Google Calendar is not configured."

        CALENDAR_API_CALLS.labels(operation="list_events").inc()
        _cal_t0 = time.time()
        if phone_age == float("inf"):
            fallback_reason = "no phone sync on record"
        elif phone_age >= 86400:
            fallback_reason = f"phone sync {int(phone_age / 3600)}h old"
        else:
            fallback_reason = "phone sync returned no parseable events"
        logger.info(
            f"[CALENDAR] {fallback_reason} — falling back to Google ({days_ahead}d)",
            extra={"component": "calendar"},
        )
        response = await client.list_events(days_ahead=days_ahead)
        CALENDAR_API_LATENCY.labels(operation="list_events").observe(time.time() - _cal_t0)

        if not response.success:
            CALENDAR_API_ERRORS.labels(operation="list_events").inc()
            return f"Calendar error: {response.error}"

        for event in response.events:
            events.append(
                {
                    "title": event.title,
                    "start": event.start,
                    "all_day": event.all_day,
                    "location": event.location,
                    "end": event.end,
                }
            )
        source = "google"

    if not events:
        if days_ahead == 1:
            return "No events on the calendar for today."
        return f"No events on the calendar for the next {days_ahead} days."

    # All-day events first, then chronological by start
    events.sort(key=lambda e: (not e["all_day"], e["start"]))

    lines = []
    header_suffix = f" [{source}]" if source else ""
    if days_ahead == 1:
        lines.append(f"Today's calendar ({len(events)} events){header_suffix}:")
    else:
        lines.append(f"Calendar for the next {days_ahead} days ({len(events)} events){header_suffix}:")

    for ev in events:
        if ev["all_day"]:
            date_str = ev["start"].strftime("%A %b %d")
            lines.append(f"\n- {ev['title']} (all day, {date_str})")
        else:
            time_str = ev["start"].strftime("%A %b %d, %I:%M %p")
            if ev.get("end"):
                end_str = ev["end"].strftime("%I:%M %p")
                lines.append(f"\n- {ev['title']} — {time_str} to {end_str}")
            else:
                lines.append(f"\n- {ev['title']} — {time_str}")
        if ev.get("location"):
            lines.append(f"  Location: {ev['location']}")

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
    from orchestrator.shared import profile

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
        # F-011 + F-013: push channels run in parallel with HA Companion push.
        # Each is individually gated by its own *_enabled flag — no-op when
        # off. Dispatched as detached tasks so a slow push server never
        # extends the scheduler job duration (prod-support review of F-011).
        try:
            import asyncio as _asyncio

            from orchestrator.reminder_manager import deliver_via_ntfy

            async def _ntfy_and_log() -> None:
                try:
                    await deliver_via_ntfy(reminder_id, text)
                except Exception as push_err:
                    logger.error(
                        f"[REMINDER] ntfy push raised for {reminder_id}: {push_err}",
                        extra={"component": "reminder"},
                        exc_info=True,
                    )

            _asyncio.create_task(_ntfy_and_log())
        except Exception as ntfy_err:
            logger.error(
                f"[REMINDER] ntfy dispatch failed for {reminder_id}: {ntfy_err}",
                extra={"component": "reminder"},
                exc_info=True,
            )

        # F-013 Pushover push — parallel channel with more reliable iOS APNs
        # delivery. Same detached-task pattern.
        try:
            import asyncio as _asyncio_po

            from orchestrator.pushover_manager import deliver_via_pushover

            async def _pushover_and_log() -> None:
                try:
                    await deliver_via_pushover(reminder_id, text)
                except Exception as push_err:
                    logger.error(
                        f"[REMINDER] pushover push raised for {reminder_id}: {push_err}",
                        extra={"component": "reminder"},
                        exc_info=True,
                    )

            _asyncio_po.create_task(_pushover_and_log())
        except Exception as po_err:
            logger.error(
                f"[REMINDER] pushover dispatch failed for {reminder_id}: {po_err}",
                extra={"component": "reminder"},
                exc_info=True,
            )

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
            retry_time = datetime.now(ZoneInfo(shared.TIMEZONE)) + timedelta(minutes=2)
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
    from orchestrator.finance_manager import (
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
                from orchestrator.selfcare_manager import get_selfcare_status

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
                from orchestrator.prompt_builder import rag_context

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
    from orchestrator.vision_handler import analyze_image

    if not shared._vision_image_cache:
        return "No image available. The user needs to share an image first."

    # Use the most recently cached image
    last_key = list(shared._vision_image_cache.keys())[-1]
    image_data = shared._vision_image_cache[last_key]

    logger.info("[VISION_TOOL] Re-analyzing cached image with query: %s", query[:100])
    return await analyze_image(image_data, query)


def _handle_document_vault(arguments: Dict[str, Any]) -> str:
    """Handle document_vault tool calls."""
    from orchestrator.state_store import get_document, list_documents, save_document, update_document

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
                from orchestrator import shared

                rag_id = doc.get("rag_doc_id") or f"vault_{doc_id}"
                notes_id = f"{rag_id}_notes"
                # Route to palace wing/room based on document category
                wing_map = {
                    "financial": ("personal", "finance"),
                    "medical": ("personal", "health"),
                }
                doc_wing, doc_room = wing_map.get(doc["category"], ("personal", ""))
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
                            "wing": doc_wing,
                            "room": doc_room,
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
        from datetime import datetime

        doc_id = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
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
            from orchestrator import shared

            wing_map = {
                "financial": ("personal", "finance"),
                "medical": ("personal", "health"),
            }
            doc_wing, doc_room = wing_map.get(category, ("personal", ""))
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
                        "wing": doc_wing,
                        "room": doc_room,
                    }
                ],
                ids=[f"vault_{doc_id}"],
            )
        except Exception as e:
            logger.warning(f"[DOCVAULT] RAG indexing failed for new doc: {e}")

        return f'Created document "{title}" (id: {doc_id}, category: {category}). It\'s saved and searchable.'

    return f"Unknown action: {action}"


# ---------------------------------------------------------------------------
# Tool registry registrations
# Migrated tools use @register_tool and go through the registry pipeline
# instead of the legacy if-elif chain above.
# ---------------------------------------------------------------------------
from orchestrator.tool_registry import register_tool


@register_tool("search_memory")
def _reg_search_memory(arguments: dict) -> str:
    return tool_search_memory(
        arguments.get("query", ""),
        wing=arguments.get("wing", ""),
        room=arguments.get("room", ""),
    )


@register_tool("home_assistant")
async def _reg_home_assistant(arguments: dict) -> str:
    return await tool_home_assistant(
        arguments.get("entity_id", ""), arguments.get("service", ""), arguments.get("data", {})
    )


@register_tool("update_data")
def _reg_update_data(arguments: dict) -> str:
    return tool_update_data(arguments)


@register_tool("update_memory")
async def _reg_update_memory(arguments: dict) -> str:
    from orchestrator.memory_manager import update_memory

    return await update_memory(
        correction=arguments.get("correction", ""),
        search_query=arguments.get("search_query", ""),
        category=arguments.get("category", "general"),
    )


@register_tool("set_reminder")
async def _reg_set_reminder(arguments: dict) -> str:
    return await tool_set_reminder(
        arguments.get("reminder_text", ""), arguments.get("time", ""), arguments.get("target", "both")
    )


@register_tool("cancel_reminder")
async def _reg_cancel_reminder(arguments: dict) -> str:
    return await tool_cancel_reminder(arguments.get("reminder_id", ""))


@register_tool("start_focus")
async def _reg_start_focus(arguments: dict) -> str:
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


@register_tool("stop_focus")
async def _reg_stop_focus(arguments: dict) -> str:
    return await tool_stop_focus()


@register_tool("focus_status")
async def _reg_focus_status(arguments: dict) -> str:
    return await tool_focus_status()


@register_tool("focus_sprint")
async def _reg_focus_sprint(arguments: dict) -> str:
    return await tool_focus_sprint(arguments.get("action", ""), arguments.get("duration_minutes"))


@register_tool("web_search")
async def _reg_web_search(arguments: dict) -> str:
    return await tool_web_search(
        arguments.get("query", ""), arguments.get("category", "general"), arguments.get("time_range")
    )


@register_tool("check_calendar")
async def _reg_check_calendar(arguments: dict) -> str:
    return await tool_check_calendar(arguments.get("days_ahead", 7))


@register_tool("create_calendar_event")
async def _reg_create_calendar_event(arguments: dict) -> str:
    return await tool_create_calendar_event(
        arguments.get("title", ""),
        arguments.get("start_time", ""),
        arguments.get("duration_minutes", 60),
        arguments.get("description", ""),
        arguments.get("location", ""),
    )


@register_tool("check_email")
async def _reg_check_email(arguments: dict) -> str:
    return await tool_check_email(
        arguments.get("query", ""), arguments.get("max_results", 10), arguments.get("unread_only", False)
    )


@register_tool("search_email")
async def _reg_search_email(arguments: dict) -> str:
    return await tool_search_email(arguments.get("query", ""), arguments.get("max_results", 10))


@register_tool("finance_status")
async def _reg_finance_status(arguments: dict) -> str:
    return await tool_finance_status(arguments.get("include_details", False))


@register_tool("brain_dump")
async def _reg_brain_dump(arguments: dict) -> str:
    return await tool_brain_dump(arguments.get("items", []))


@register_tool("decompose_task")
async def _reg_decompose_task(arguments: dict) -> str:
    return await tool_decompose_task(
        arguments.get("task", ""), arguments.get("mode", "next_step_only"), arguments.get("context", "")
    )


@register_tool("task_step")
def _reg_task_step(arguments: dict) -> str:
    return tool_task_step(arguments.get("task_id", ""), arguments.get("action", ""))


@register_tool("decide_for_me")
async def _reg_decide_for_me(arguments: dict) -> str:
    return await tool_decide_for_me(arguments)


@register_tool("selfcare_log")
async def _reg_selfcare_log(arguments: dict) -> str:
    from orchestrator.selfcare_manager import get_selfcare_status, log_selfcare

    if arguments.get("action") == "check":
        status = await get_selfcare_status()
        return json.dumps(status)
    return await log_selfcare(arguments.get("action", ""), arguments.get("detail"))


@register_tool("bookmark_context")
async def _reg_bookmark_context(arguments: dict) -> str:
    from orchestrator.context_tracker import bookmark_context

    result = await bookmark_context(arguments.get("description"))
    desc = result["description"]
    delay = result["checkin_delay"]
    return f"Got it — bookmarking your spot. You were working on {desc}. I'll check in with you in {delay} minutes."


@register_tool("recall_context")
async def _reg_recall_context(arguments: dict) -> str:
    from orchestrator.context_tracker import get_recent_context

    entries = await get_recent_context(arguments.get("count", 3))
    if not entries:
        return "I don't have any recent context recorded yet."
    lines = ["Here's what you were working on:"]
    for i, e in enumerate(entries, 1):
        lines.append(f"{i}. {e['description']} — {e['when']}")
    return "\n".join(lines)


@register_tool("start_routine")
async def _reg_start_routine(arguments: dict) -> str:
    from orchestrator.routine_manager import start_routine

    return await start_routine(arguments.get("routine_id", ""))


@register_tool("routine_action")
async def _reg_routine_action(arguments: dict) -> str:
    from orchestrator.routine_manager import advance_step

    return await advance_step(arguments.get("action", "done"))


@register_tool("routine_status")
async def _reg_routine_status(arguments: dict) -> str:
    from orchestrator.routine_manager import get_routine_status

    return await get_routine_status()


@register_tool("query_budget")
async def _reg_query_budget(arguments: dict) -> str:
    from orchestrator import budget_manager

    result = await budget_manager.query(
        dataset=arguments.get("dataset"),
        question_type=arguments.get("question_type", "list_datasets"),
        start_date=arguments.get("start_date"),
        end_date=arguments.get("end_date"),
        category=arguments.get("category"),
        payee_contains=arguments.get("payee_contains"),
        amount_sign=arguments.get("amount_sign"),
        limit=int(arguments.get("limit", 20)),
        analysis_question=arguments.get("analysis_question"),
    )
    return json.dumps(result, default=str)


@register_tool("check_system")
async def _reg_check_system(arguments: dict) -> str:
    from orchestrator.system_diagnostics import check_system

    return await check_system(arguments.get("query", "system_health"))


@register_tool("analyze_image")
async def _reg_analyze_image(arguments: dict) -> str:
    return await tool_analyze_image(arguments.get("query", "Describe this image in detail."))


@register_tool("sleep_mode")
async def _reg_sleep_mode(arguments: dict) -> str:
    import contextlib

    from orchestrator import state_store
    from orchestrator.shared import scheduler

    action = arguments.get("action", "on")
    duration_hours = arguments.get("duration_hours")
    shared.DND_ACTIVE = action == "on"

    # Cancel any existing auto-unmute job
    with contextlib.suppress(Exception):
        scheduler.remove_job("dnd_auto_unmute")

    if shared.DND_ACTIVE:
        state_store.set_notification_flag("dnd_active")

        if duration_hours:
            # Schedule auto-unmute
            from datetime import datetime, timedelta

            unmute_at = datetime.now() + timedelta(hours=float(duration_hours))
            scheduler.add_job(
                _auto_unmute_dnd,
                "date",
                run_date=unmute_at,
                id="dnd_auto_unmute",
                replace_existing=True,
            )
            hrs = float(duration_hours)
            label = f"{hrs:.0f} hour{'s' if hrs != 1 else ''}" if hrs == int(hrs) else f"{hrs} hours"
            logger.info("[DND] Muted for %s (until %s)", label, unmute_at.strftime("%I:%M %p"))
            return (
                f"Muted all announcements for {label}. I'll unmute automatically at {unmute_at.strftime('%I:%M %p')}."
            )
        else:
            logger.info("[DND] Sleep mode enabled — all announcements suppressed")
            return "Muted. No announcements until you tell me to unmute or say good morning."
    else:
        state_store.clear_notification_flag("dnd_active")
        logger.info("[DND] Announcements resumed")
        return "Unmuted! Announcements are back on."


async def _auto_unmute_dnd():
    """Auto-unmute callback for timed DND."""
    from orchestrator import state_store

    shared.DND_ACTIVE = False
    state_store.clear_notification_flag("dnd_active")
    logger.info("[DND] Auto-unmuted after timed mute")


@register_tool("shopping_list")
async def _reg_shopping_list(arguments: dict) -> str:
    import asyncio

    return await asyncio.to_thread(_handle_shopping_list, arguments)


@register_tool("document_vault")
async def _reg_document_vault(arguments: dict) -> str:
    import asyncio

    return await asyncio.to_thread(_handle_document_vault, arguments)


@register_tool("paperless_save")
async def _reg_paperless_save(arguments: dict) -> str:
    """F-012: hand off a staged file to Paperless-ngx.

    Security: `filename` must be a bare basename (no `/`, no `..`, no null
    byte, not absolute). The resolved file path is required to live under
    settings.paperless_inbox_path — we reject anything that resolves above
    it, defending against symlink attacks inside the staging dir. Size
    capped at DOCUMENT_MAX_SIZE_MB (default 100MB) before reading to
    prevent OOM from a staged huge file.
    """
    import asyncio
    import os
    from pathlib import Path

    from orchestrator.config import settings
    from orchestrator.paperless_manager import upload_file

    if not settings.paperless_enabled:
        return (
            "Paperless bridge is disabled. Set PAPERLESS_ENABLED=true plus "
            "PAPERLESS_URL and PAPERLESS_API_TOKEN in .env to turn it on."
        )

    filename = (arguments.get("filename") or "").strip()
    if not filename:
        return "Please tell me which filename in the inbox to save."

    # Basename-only check — no separators, no parent refs, no absolute path,
    # no null byte (CPython's path layer already rejects \x00 with ValueError,
    # but surfacing a clean error here is friendlier to the LLM).
    if "\x00" in filename:
        return "Refused filename: contains a null byte."
    if "/" in filename or "\\" in filename or ".." in filename:
        return f"Refused filename '{filename}': pass only the basename (e.g. 'tax-q3-2026.pdf'), no paths."
    if os.path.isabs(filename):
        return f"Refused absolute path '{filename}': pass only the basename."

    inbox = Path(settings.paperless_inbox_path).resolve()
    try:
        candidate = (inbox / filename).resolve()
    except Exception as e:
        return f"Couldn't resolve path for '{filename}': {e}"

    # Post-resolve sanity: the resolved path MUST sit inside the inbox root.
    # If a symlink inside the inbox points outside, this rejects it.
    try:
        candidate.relative_to(inbox)
    except ValueError:
        return f"Refused '{filename}': resolved path escapes the Paperless inbox."

    if not candidate.is_file():
        return f"No file named '{filename}' in the Paperless inbox. Drop it in {settings.paperless_inbox_path} first."

    # Size guard BEFORE reading. The REST route relies on RequestSizeLimit-
    # Middleware's 100MB cap; the tool path bypasses middleware entirely
    # (file is already on disk), so we enforce the same ceiling here to
    # protect orchestrator RAM from a staged huge file + LLM-driven
    # paperless_save call. MAX_UPLOAD_SIZE is parsed from DOCUMENT_MAX_SIZE_MB.
    try:
        size = candidate.stat().st_size
    except OSError as stat_err:
        return f"Couldn't stat '{filename}': {stat_err}"
    max_bytes = int(os.environ.get("DOCUMENT_MAX_SIZE_MB", "100")) * 1024 * 1024
    if size > max_bytes:
        from orchestrator.metrics import PAPERLESS_UPLOAD_TOTAL

        PAPERLESS_UPLOAD_TOTAL.labels(result="skipped", reason="file_too_large").inc()
        return f"Refused '{filename}': {size / 1_048_576:.1f} MB exceeds the {max_bytes // 1_048_576} MB upload cap."

    # Read bytes off the event loop — up to the cap enforced above, this
    # can still be tens of MB and blocks the loop otherwise.
    try:
        content = await asyncio.to_thread(candidate.read_bytes)
    except Exception as e:
        return f"Couldn't read '{filename}': {type(e).__name__}: {e}"

    tags = arguments.get("tags") or None
    if tags is not None and not isinstance(tags, list):
        # Defensive: LLM occasionally passes a comma string
        tags = [str(t).strip() for t in str(tags).split(",") if str(t).strip()]

    result = await upload_file(
        content=content,
        filename=filename,
        title=arguments.get("title") or None,
        correspondent=arguments.get("correspondent") or None,
        document_type=arguments.get("document_type") or None,
        tags=tags,
    )

    if result.get("success"):
        return (
            f"Queued '{filename}' to Paperless. Task id: "
            f"{str(result.get('task_id'))[:60]}. OCR and tagging will finish "
            "in a moment; find it in the Paperless web UI."
        )

    if result.get("skipped"):
        return f"Paperless bridge skipped the upload: {result.get('reason')}"

    if result.get("status_code"):
        return f"Paperless rejected '{filename}' (HTTP {result.get('status_code')}): {result.get('body', '')}"

    return f"Paperless upload failed for '{filename}': {result.get('error')}"


@register_tool("check_claude_activity")
def _reg_check_claude_activity(arguments: dict) -> str:
    from orchestrator.claude_code_tracker import (
        get_current_session_turns,
        get_files_touched,
        get_recent_activity_summary,
    )

    action = arguments.get("action", "recent")
    minutes_back = int(arguments.get("minutes_back") or 120)

    if action == "recent":
        summary = get_recent_activity_summary(minutes_back=minutes_back)
        return summary or f"No Claude Code activity in the last {minutes_back} minutes."

    if action == "current_session":
        turns = get_current_session_turns(n=10)
        if not turns:
            return "No active Claude Code session found."
        lines = ["Current Claude Code session (last 10 turns):"]
        for turn in turns:
            ts = (turn.get("timestamp") or "")[:16]
            turn_type = turn.get("turn_type", "")
            content = (turn.get("content") or "")[:200]
            tool_uses = turn.get("tool_uses") or []
            files = turn.get("files_touched") or []
            prefix = f"  [{ts}] {turn_type}:"
            if content:
                lines.append(f"{prefix} {content.splitlines()[0][:200]}")
            if tool_uses:
                lines.append(f"    tools: {', '.join(tool_uses[:5])}")
            if files:
                lines.append(f"    files: {', '.join(files[:5])}")
        return "\n".join(lines)

    if action == "files_touched":
        files = get_files_touched(minutes_back=minutes_back)
        if not files:
            return f"No files touched by Claude Code in the last {minutes_back} minutes."
        return "Files touched by Claude Code:\n" + "\n".join(f"  - {f}" for f in files[:30])

    return f"Unknown action: {action}"


# Import code_agent to register its @register_tool handler
import contextlib

with contextlib.suppress(ImportError):
    from orchestrator import code_agent  # noqa: F401

# Import expert_agent to register the ask_expert handler
with contextlib.suppress(ImportError):
    from orchestrator import expert_agent  # noqa: F401


# ---------------------------------------------------------------------------
# Workouts
# ---------------------------------------------------------------------------


@register_tool("generate_workout")
async def _reg_generate_workout(arguments: dict) -> str:
    import asyncio

    from orchestrator.workout_manager import generate_workout

    result = await asyncio.to_thread(generate_workout)
    if not result.get("ok"):
        return json.dumps(result)
    # Build a compact plan description for the model's context
    lines = [
        f"Workout ready (id={result['workout_id']}, type={result['workout_type']}).",
        f"Why: {result['reasoning']}",
        "Plan:",
    ]
    for ex in result.get("exercises", []):
        sets = ex["sets"]
        first = sets[0]
        target_reps = first.get("target_reps")
        target_w = first.get("target_weight_lbs")
        weight_txt = f"{target_w:.0f} lb" if target_w else "user weight"
        lines.append(f"  - {ex['name']} ({ex['primary_muscle']}): {len(sets)}x{target_reps} @ {weight_txt}")
    lines.append("(Saved to the dashboard. User asked at the gym — do not read this aloud.)")
    return "\n".join(lines)


@register_tool("log_set")
async def _reg_log_set(arguments: dict) -> str:
    import asyncio

    from orchestrator.workout_manager import log_set

    exercise = str(arguments.get("exercise") or "").strip()[:200]
    if not exercise:
        return "Exercise name is required."
    try:
        weight = float(arguments.get("weight_lbs"))
        reps = int(arguments.get("reps"))
    except (TypeError, ValueError):
        return "weight_lbs and reps are required (numeric)."
    if not (0 < weight <= 2000):
        return "weight_lbs must be between 0 and 2000."
    if not (0 < reps <= 1000):
        return "reps must be between 1 and 1000."
    rpe_raw = arguments.get("rpe")
    rpe: float | None = None
    if rpe_raw not in (None, ""):
        try:
            rpe = float(rpe_raw)
        except (TypeError, ValueError):
            rpe = None
        if rpe is not None and not (0 < rpe <= 10):
            rpe = None

    result = await asyncio.to_thread(log_set, exercise, weight, reps, rpe)
    return (
        f"Logged: {exercise} {weight:.0f} lb × {reps}"
        + (f" @ RPE {rpe}" if rpe else "")
        + f" (workout {result['workout_id']})."
    )


@register_tool("workout_status")
async def _reg_workout_status(arguments: dict) -> str:
    import asyncio

    from orchestrator.workout_manager import get_history, get_status

    action = arguments.get("action", "today")
    if action == "history":
        days = int(arguments.get("days") or 7)
        history = await asyncio.to_thread(get_history, days)
        return json.dumps({"days": days, "sessions": history})
    # default: today
    status = await asyncio.to_thread(get_status)
    return json.dumps(status)


@register_tool("modify_workout")
async def _reg_modify_workout(arguments: dict) -> str:
    import asyncio

    from orchestrator.workout_manager import modify_workout

    workout_id = arguments.get("workout_id")
    if not workout_id:
        return "workout_id is required."
    remove = arguments.get("remove_exercises") or []
    add = arguments.get("add_exercises") or []
    result = await asyncio.to_thread(modify_workout, int(workout_id), remove, add)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------


@register_tool("log_meal")
async def _reg_log_meal(arguments: dict) -> str:
    import asyncio

    from orchestrator.meal_manager import log_meal

    action = arguments.get("action", "log")

    if action == "today":
        from orchestrator.meal_manager import get_today

        today = await asyncio.to_thread(get_today)
        return json.dumps(today)

    description = str(arguments.get("description") or "").strip()
    if not description:
        return "description is required to log a meal."
    calories_raw = arguments.get("calories")
    try:
        calories = int(calories_raw) if calories_raw not in (None, "") else None
    except (TypeError, ValueError):
        calories = None
    meal_type = arguments.get("meal_type")

    result = await asyncio.to_thread(log_meal, description, calories, meal_type, None, "voice")
    if not result.get("ok"):
        return result.get("error", "Failed to log meal.")
    meal = result["meal"]
    cal_txt = f" ({meal['calories']} kcal)" if meal.get("calories") else ""
    return f"Logged {meal['meal_type']}: {meal['description']}{cal_txt}."
