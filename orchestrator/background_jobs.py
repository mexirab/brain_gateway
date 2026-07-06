"""
Background scheduler jobs — thin re-export facade.

All implementations live in domain-specific modules:
- jobs_calendar: calendar polling, morning + evening briefings, email polling, email-to-calendar
- jobs_finance: YNAB sync, weekly spending summary, mid-month budget warning
- jobs_monitoring: temperature, ambient awareness, self-care, routines, progress
- jobs_winddown: sleep wind-down ladder rungs (T-60 lights, T-30 nudge)
"""

# Re-exports for backward compatibility
from orchestrator.jobs_calendar import (  # noqa: F401
    _get_prep_hint,
    _get_weather_forecast,
    _is_focus_related,
    _parse_event_json,
    _parse_phone_datetime,
    evening_briefing,
    morning_briefing,
    poll_calendar,
    process_emails_for_events,
)
from orchestrator.jobs_finance import (  # noqa: F401
    midmonth_budget_warning,
    sync_ynab_transactions,
    weekly_spending_summary,
)
from orchestrator.jobs_monitoring import (  # noqa: F401
    ambient_summary,
    check_closet_temperature,
    check_selfcare,
    daily_progress_summary,
    trigger_routine,
    update_ambient_led,
    weekly_backlog_review,
    weekly_progress_digest,
)
from orchestrator.jobs_winddown import (  # noqa: F401
    wind_down_dim,
    wind_down_nudge,
)

__all__ = [
    # Calendar & email
    "_get_weather_forecast",
    "_get_prep_hint",
    "_is_focus_related",
    "_parse_phone_datetime",
    "_parse_event_json",
    "evening_briefing",
    "morning_briefing",
    "poll_calendar",
    "process_emails_for_events",
    # Finance
    "sync_ynab_transactions",
    "weekly_spending_summary",
    "midmonth_budget_warning",
    # Monitoring & routines
    "check_closet_temperature",
    "ambient_summary",
    "update_ambient_led",
    "check_selfcare",
    "trigger_routine",
    "daily_progress_summary",
    "weekly_progress_digest",
    "weekly_backlog_review",
    # Wind-down ladder
    "wind_down_dim",
    "wind_down_nudge",
]
