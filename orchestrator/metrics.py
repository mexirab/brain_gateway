"""
Brain Gateway Prometheus Metrics

All metric definitions in one place. Import individual metrics where needed.
Uses bgw_ prefix to namespace all metrics.
"""

from prometheus_client import Counter, Gauge, Histogram, Info

# -- Request-level metrics ---------------------------------------------------
REQUEST_COUNT = Counter(
    "bgw_requests_total",
    "Total chat completion requests",
    ["mode"],  # hybrid, fallback, passthrough, fast_path
)

REQUEST_LATENCY = Histogram(
    "bgw_request_duration_seconds",
    "End-to-end request latency",
    ["mode"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)

REQUEST_ERRORS = Counter(
    "bgw_request_errors_total",
    "Total request errors",
    ["mode", "error_type"],
)

ACTIVE_REQUESTS = Gauge(
    "bgw_active_requests",
    "Currently processing requests",
)

# -- LLM call metrics -------------------------------------------------------
LLM_CALL_COUNT = Counter(
    "bgw_llm_calls_total",
    "Total LLM API calls",
    ["model", "purpose"],  # model: helios/nemotron, purpose: conversation/tool_loop/expert/final
)

LLM_CALL_LATENCY = Histogram(
    "bgw_llm_call_duration_seconds",
    "LLM call latency",
    ["model", "purpose"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 180.0, 300.0],
)

LLM_CALL_ERRORS = Counter(
    "bgw_llm_call_errors_total",
    "LLM call failures",
    ["model", "error_type"],
)

# -- Tool execution metrics --------------------------------------------------
TOOL_CALL_COUNT = Counter(
    "bgw_tool_calls_total",
    "Total tool executions",
    ["tool"],
)

TOOL_CALL_LATENCY = Histogram(
    "bgw_tool_call_duration_seconds",
    "Tool execution latency",
    ["tool"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

TOOL_CALL_ERRORS = Counter(
    "bgw_tool_call_errors_total",
    "Tool execution failures",
    ["tool"],
)

TOOL_ROUNDS = Histogram(
    "bgw_tool_loop_rounds",
    "Number of Nemotron tool loop rounds per request",
    buckets=[1, 2, 3, 4, 5],
)

# -- Mode Router metrics -----------------------------------------------------
MODE_ROUTE_COUNT = Counter(
    "bgw_mode_route_total",
    "Mode router classifications",
    ["mode", "intensity"],
)

# -- RAG metrics -------------------------------------------------------------
RAG_QUERY_COUNT = Counter(
    "bgw_rag_queries_total",
    "Total RAG queries",
)

RAG_QUERY_LATENCY = Histogram(
    "bgw_rag_query_duration_seconds",
    "RAG query latency (embedding + ChromaDB)",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0],
)

RAG_RESULTS_RETURNED = Histogram(
    "bgw_rag_results_count",
    "Number of RAG chunks returned per query",
    buckets=[0, 1, 2, 3, 5, 10, 15, 25],
)

# -- Focus Timer metrics -----------------------------------------------------
FOCUS_SESSIONS_STARTED = Counter(
    "bgw_focus_sessions_started_total",
    "Focus sessions started",
    ["soundscape"],
)

FOCUS_SESSIONS_COMPLETED = Counter(
    "bgw_focus_sessions_completed_total",
    "Focus sessions that ran to completion (not stopped early)",
)

FOCUS_SESSIONS_STOPPED_EARLY = Counter(
    "bgw_focus_sessions_stopped_early_total",
    "Focus sessions stopped before completion",
)

FOCUS_SESSION_DURATION = Histogram(
    "bgw_focus_session_actual_minutes",
    "Actual focus session duration in minutes",
    buckets=[5, 10, 15, 20, 25, 30, 45, 60, 90, 120],
)

FOCUS_ACTIVE = Gauge(
    "bgw_focus_active",
    "Whether a focus session is currently active (1=yes, 0=no)",
)

# -- Helios Power Management -------------------------------------------------
HELIOS_ONLINE = Gauge(
    "bgw_helios_online",
    "Whether Helios is currently online (1=yes, 0=no)",
)

HELIOS_START_COUNT = Counter(
    "bgw_helios_starts_total",
    "Number of times Helios was auto-started",
)

HELIOS_STOP_COUNT = Counter(
    "bgw_helios_stops_total",
    "Number of times Helios was auto-stopped",
)

HELIOS_START_LATENCY = Histogram(
    "bgw_helios_start_duration_seconds",
    "Time to start Helios and get it ready",
    buckets=[5, 10, 20, 30, 60, 90, 120, 180],
)

HELIOS_IDLE_SECONDS = Gauge(
    "bgw_helios_idle_seconds",
    "Seconds since last Helios request",
)

# -- Reminders ---------------------------------------------------------------
REMINDERS_SET = Counter(
    "bgw_reminders_set_total",
    "Reminders created",
    ["target"],
)

REMINDERS_DELIVERED = Counter(
    "bgw_reminders_delivered_total",
    "Reminders successfully delivered",
)

REMINDERS_PENDING = Gauge(
    "bgw_reminders_pending",
    "Currently pending reminders",
)

# -- Calendar ----------------------------------------------------------------
CALENDAR_API_CALLS = Counter(
    "bgw_calendar_api_calls_total",
    "Google Calendar API calls",
    ["operation"],
)

CALENDAR_API_LATENCY = Histogram(
    "bgw_calendar_api_duration_seconds",
    "Google Calendar API latency",
    ["operation"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)

CALENDAR_API_ERRORS = Counter(
    "bgw_calendar_api_errors_total",
    "Google Calendar API errors",
    ["operation"],
)

CALENDAR_POLL_EVENTS_FOUND = Counter(
    "bgw_calendar_poll_events_announced_total",
    "Events announced by calendar poller",
)

# -- Gmail -------------------------------------------------------------------
GMAIL_API_CALLS = Counter(
    "bgw_gmail_api_calls_total",
    "Gmail API calls",
    ["operation"],
)

GMAIL_API_LATENCY = Histogram(
    "bgw_gmail_api_duration_seconds",
    "Gmail API latency",
    ["operation"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)

GMAIL_API_ERRORS = Counter(
    "bgw_gmail_api_errors_total",
    "Gmail API errors",
    ["operation"],
)

# -- Pi-hole -----------------------------------------------------------------
PIHOLE_BLOCKING_TOGGLES = Counter(
    "bgw_pihole_blocking_toggles_total",
    "Pi-hole focus blocking enable/disable",
    ["action"],
)

# -- Web Search --------------------------------------------------------------
WEB_SEARCH_COUNT = Counter(
    "bgw_web_search_total",
    "Web search queries",
)

WEB_SEARCH_LATENCY = Histogram(
    "bgw_web_search_duration_seconds",
    "Web search latency",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 15.0],
)

WEB_SEARCH_RESULTS = Histogram(
    "bgw_web_search_results_count",
    "Number of web search results returned",
    buckets=[0, 1, 2, 3, 5, 10],
)

# -- Fast Path ---------------------------------------------------------------
FAST_PATH_COUNT = Counter(
    "bgw_fast_path_total",
    "Requests handled by fast path",
    ["action"],
)

FAST_PATH_BYPASS = Counter(
    "bgw_fast_path_bypass_total",
    "Requests that could not use fast path (fell through to LLM)",
)

# -- Email-to-Calendar -------------------------------------------------------
EMAIL_TO_CALENDAR_EVENTS_CREATED = Counter(
    "bgw_email_to_calendar_events_created_total",
    "Calendar events auto-created from emails",
)

EMAIL_TO_CALENDAR_EMAILS_SCANNED = Counter(
    "bgw_email_to_calendar_emails_scanned_total",
    "Emails scanned for calendar events",
)

# -- Temperature Monitoring --------------------------------------------------
TEMPERATURE_GAUGE = Gauge(
    "bgw_temperature_fahrenheit",
    "Temperature sensor readings in Fahrenheit",
    ["location"],  # closet, kitchen
)

TEMPERATURE_DELTA = Gauge(
    "bgw_temperature_delta_fahrenheit",
    "Temperature difference between server closet and ambient (kitchen)",
)

# -- System info -------------------------------------------------------------
BUILD_INFO = Info(
    "bgw_build",
    "Brain Gateway build information",
)

# -- Auto-Learn ---------------------------------------------------------------
AUTO_LEARN_EXTRACTIONS_TOTAL = Counter(
    "bgw_auto_learn_extractions_total",
    "Auto-learn extraction jobs run",
)

AUTO_LEARN_FACTS_STORED = Counter(
    "bgw_auto_learn_facts_stored_total",
    "Facts stored by auto-learn",
    ["category"],
)

AUTO_LEARN_DUPLICATES_SKIPPED = Counter(
    "bgw_auto_learn_duplicates_skipped_total",
    "Facts skipped by auto-learn dedup",
)

AUTO_LEARN_SENSITIVE_FILTERED = Counter(
    "bgw_auto_learn_sensitive_filtered_total",
    "Facts rejected by sensitive data filter",
)

AUTO_LEARN_EXTRACTION_LATENCY = Histogram(
    "bgw_auto_learn_extraction_duration_seconds",
    "Auto-learn extraction pipeline latency",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)
