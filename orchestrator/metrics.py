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
    ["mode"],  # unified, unified_fallback, fast_path
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
    ["model", "purpose"],  # model: primary/fallback, purpose: conversation/tool_loop/final
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
    "Number of tool loop rounds per request",
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

# -- Model Health -----------------------------------------------------------
HELIOS_ONLINE = Gauge(
    "bgw_helios_online",
    "Whether the primary model is currently online (1=yes, 0=no)",
)

FALLBACK_ONLINE = Gauge(
    "bgw_fallback_online",
    "Whether the fallback model is currently online (1=yes, 0=no)",
)

MODEL_SERVER_START_COUNT = Counter(
    "bgw_model_server_starts_total",
    "Number of times the model server was auto-started via SSH",
)
# Backward-compat aliases for existing imports
HELIOS_START_COUNT = MODEL_SERVER_START_COUNT

MODEL_SERVER_STOP_COUNT = Counter(
    "bgw_model_server_stops_total",
    "Number of times the model server was auto-stopped via SSH",
)
HELIOS_STOP_COUNT = MODEL_SERVER_STOP_COUNT

MODEL_SERVER_START_LATENCY = Histogram(
    "bgw_model_server_start_duration_seconds",
    "Time to start the model server and get it ready",
    buckets=[5, 10, 20, 30, 60, 90, 120, 180],
)
HELIOS_START_LATENCY = MODEL_SERVER_START_LATENCY

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

# -- Brain Dump ---------------------------------------------------------------
BRAIN_DUMP_ITEMS_CAPTURED = Counter(
    "bgw_brain_dump_items_captured_total",
    "Brain dump items captured",
    ["category"],
)

BRAIN_DUMP_ITEMS_ROUTED = Counter(
    "bgw_brain_dump_items_routed_total",
    "Brain dump items routed to destination",
    ["destination"],  # rag, reminder
)

BRAIN_DUMP_RAG_LATENCY = Histogram(
    "bgw_brain_dump_rag_duration_seconds",
    "Brain dump RAG upsert latency (embedding + dedup + store)",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)

BRAIN_DUMP_DUPLICATES_SKIPPED = Counter(
    "bgw_brain_dump_duplicates_skipped_total",
    "Brain dump items skipped by dedup",
)

BRAIN_DUMP_ERRORS = Counter(
    "bgw_brain_dump_errors_total",
    "Brain dump processing errors",
    ["operation"],  # route, rag_upsert
)

# -- Task Decomposition -------------------------------------------------------
TASK_DECOMP_TASKS_CREATED = Counter(
    "bgw_task_decomp_tasks_created_total",
    "Tasks decomposed into micro-steps",
)

TASK_DECOMP_STEPS_COMPLETED = Counter(
    "bgw_task_decomp_steps_completed_total",
    "Task decomposition steps completed",
)

TASK_DECOMP_STEPS_SKIPPED = Counter(
    "bgw_task_decomp_steps_skipped_total",
    "Task decomposition steps skipped",
)

TASK_DECOMP_TASKS_ABANDONED = Counter(
    "bgw_task_decomp_tasks_abandoned_total",
    "Decomposed tasks abandoned",
)

TASK_DECOMP_ERRORS = Counter(
    "bgw_task_decomp_errors_total",
    "Task decomposition errors",
)

# -- Progress Tracking -------------------------------------------------------
# -- TTS Announcements -------------------------------------------------------
TTS_ANNOUNCEMENTS_TOTAL = Counter(
    "bgw_tts_announcements_total",
    "TTS announcements delivered",
    ["type", "speaker", "success"],
)

TTS_LATENCY = Histogram(
    "bgw_tts_latency_seconds",
    "TTS announcement end-to-end latency (synthesis + delivery)",
    buckets=[0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0],
)

TTS_FALLBACK_TOTAL = Counter(
    "bgw_tts_fallback_total",
    "TTS announcements that used fallback speaker",
)

TTS_ERRORS_TOTAL = Counter(
    "bgw_tts_errors_total",
    "TTS announcement failures",
    ["error_type"],
)

PROGRESS_EVENTS_RECORDED = Counter(
    "bgw_progress_events_total",
    "Progress events recorded",
    ["event_type"],
)

PROGRESS_STREAK_MILESTONES = Counter(
    "bgw_progress_streak_milestones_total",
    "Streak milestone announcements triggered",
    ["category"],
)

# -- Vision / Image Recognition -----------------------------------------------
VISION_REQUEST_COUNT = Counter(
    "bgw_vision_requests_total",
    "Vision model analysis requests",
    ["status"],  # success, error, disabled
)

VISION_REQUEST_LATENCY = Histogram(
    "bgw_vision_request_duration_seconds",
    "Vision model request latency",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0],
)

VISION_IMAGE_SIZE = Histogram(
    "bgw_vision_image_size_bytes",
    "Size of images sent to vision model",
    buckets=[10_000, 50_000, 100_000, 500_000, 1_000_000, 5_000_000, 10_000_000],
)
