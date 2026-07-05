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

# Dead-man's-switch: stamped each time the scheduler fires the daily morning
# briefing. If the orchestrator/scheduler stops firing it, this goes stale and
# the MorningBriefingStale alert pages — the detector for the silent-outage
# class (a down orchestrator looked like a calendar token problem for 2 months).
MORNING_BRIEFING_LAST_RUN = Gauge(
    "bgw_morning_briefing_last_run_timestamp_seconds",
    "Unix timestamp the morning briefing job last fired",
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

# Task backlog (durable to-do list)
TASKS_CREATED = Counter("bgw_tasks_created_total", "Tasks added to the backlog", ["source"])
TASKS_COMPLETED = Counter("bgw_tasks_completed_total", "Tasks marked done")
TASKS_DROPPED = Counter("bgw_tasks_dropped_total", "Tasks dropped (no-guilt removal)")
TASKS_OPEN = Gauge("bgw_tasks_open", "Currently open backlog tasks")

REMINDERS_FAILED = Counter(
    "bgw_reminders_failed_total",
    "Reminders that exhausted every delivery channel and were marked failed",
)

REMINDERS_MISSED = Counter(
    "bgw_reminders_missed_total",
    "Reminders found >24h past due at startup and marked missed instead of delivered",
)

# -- ntfy feedback loop (F-011) ---------------------------------------------
NTFY_PUSH_TOTAL = Counter(
    "bgw_ntfy_push_total",
    "ntfy push attempts from reminder_manager",
    # result: ok | fail | skipped
    # kind:   reminder | confirm
    ["result", "kind"],
)

NTFY_PUSH_LATENCY = Histogram(
    "bgw_ntfy_push_latency_seconds",
    "Latency of the POST to the ntfy server (orchestrator -> ntfy, not phone delivery)",
    ["kind"],  # reminder | confirm — so ack-latency SLOs don't mix cosmetic confirms
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)

NTFY_ACK_TOTAL = Counter(
    "bgw_ntfy_ack_total",
    "Reminder ack callbacks from ntfy action buttons",
    ["inferred_action"],  # medication | meal | water | movement | none
)

NTFY_SNOOZE_TOTAL = Counter(
    "bgw_ntfy_snooze_total",
    "Reminder snooze callbacks from ntfy action buttons",
)

NTFY_CALLBACK_REJECTED_TOTAL = Counter(
    "bgw_ntfy_callback_rejected_total",
    "ntfy callbacks rejected before state mutation",
    ["reason"],  # bad_signature | expired | not_found | over_snoozed | signing_disabled
)

# End-to-end response latency KPI for F-011: how long from reminder fire to
# user tapping Done on the ntfy notification. Only observed when an ack
# callback resolves the reminder (not on replays or retries).
REMINDER_ACK_LATENCY = Histogram(
    "bgw_reminder_ack_latency_seconds",
    "Time between reminder trigger_time and user acknowledgement via ntfy",
    buckets=[5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200],
)

# -- Paperless bridge (F-012) -----------------------------------------------
PAPERLESS_UPLOAD_TOTAL = Counter(
    "bgw_paperless_upload_total",
    "Uploads attempted to Paperless-ngx",
    # result: ok | fail | skipped
    # reason: ok | http_4xx | http_5xx | timeout | connect_error | other |
    #         disabled | missing_url | missing_token | file_too_large |
    #         file_missing
    ["result", "reason"],
)

PAPERLESS_UPLOAD_LATENCY = Histogram(
    "bgw_paperless_upload_latency_seconds",
    "Orchestrator → Paperless upload round-trip latency",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0],
)

# -- Pushover bridge (F-013) ------------------------------------------------
PUSHOVER_PUSH_TOTAL = Counter(
    "bgw_pushover_push_total",
    "Pushover push attempts",
    # result: ok | fail | skipped
    # kind:   reminder | confirm
    # reason: ok | http_4xx | http_5xx | timeout | connect_error | other |
    #         disabled | missing_user_key | missing_app_token
    # (reason lets Grafana distinguish 'token revoked' from 'Pushover down')
    ["result", "kind", "reason"],
)

PUSHOVER_PUSH_LATENCY = Histogram(
    "bgw_pushover_push_latency_seconds",
    "Latency of the POST to the Pushover API",
    ["kind"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)

# -- Self-audit (F-014) -----------------------------------------------------
SELF_AUDIT_RUNS_TOTAL = Counter(
    "bgw_self_audit_runs_total",
    "Daily self-audit runs",
    # result: ok | partial | failed | skipped | busy
    # ok      = full audit + diagnosis succeeded (or genuinely empty week)
    # partial = clusters captured but Jess unavailable for diagnosis
    # failed  = Loki unreachable or probe empty -- digest pushed as alert,
    #           NOT as 'all clean' (so the user knows visibility is broken)
    # skipped = SELF_AUDIT_ENABLED=false
    # busy    = another audit in flight (manual + cron collision)
    ["result"],
)

SELF_AUDIT_CLUSTERS_TOTAL = Counter(
    "bgw_self_audit_clusters_total",
    "Error clusters surfaced per audit, by severity",
    # service label dropped: aggregate-only counts today, populating per-cluster
    # service would explode label cardinality without enough Grafana payoff.
    ["severity"],
)

SELF_AUDIT_LATENCY = Histogram(
    "bgw_self_audit_latency_seconds",
    "Self-audit job duration",
    buckets=[1, 5, 10, 30, 60, 120, 300],
)

SELF_AUDIT_FORMAT_DRIFT_TOTAL = Counter(
    "bgw_self_audit_format_drift_total",
    "Audits where diagnosis text was non-empty but no severity headers parsed "
    "-- signals Qwen3.5-27B went off-format and the prompt may need tuning",
)

# -- Helios wake-on-demand (PT-C) --------------------------------------------
HELIOS_WAKE_TOTAL = Counter(
    "bgw_helios_wake_total",
    "Helios power-on (plug turn_on) attempts via Home Assistant",
    # result: ok | debounced | disabled | error
    ["result"],
)

HELIOS_SLEEP_TOTAL = Counter(
    "bgw_helios_sleep_total",
    "Helios power-cut (plug turn_off) attempts via Home Assistant",
    # result: ok | disabled | error
    ["result"],
)

HELIOS_STATUS_TOTAL = Counter(
    "bgw_helios_status_total",
    "Helios power-state reads via Home Assistant",
    # result: ok | disabled | error
    ["result"],
)

HELIOS_PLUG_WATTS = Gauge(
    "bgw_helios_plug_watts",
    # Refreshed by the helios_status_poll scheduler job (every 60s when
    # HELIOS_WAKE_ENABLED) plus any on-demand /api/helios/power read; only set
    # when watts parses, so it never reports a misleading 0 on a failed read.
    "Last-known Helios smart-plug power draw in watts (from HA sensor)",
)

HELIOS_RUNNING = Gauge(
    "bgw_helios_running",
    "Whether Helios is inferred to be running from the plug state (1=running, "
    "0=asleep/unknown). Refreshed by the helios_status_poll job + on-demand reads.",
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

# -- MemPalace ---------------------------------------------------------------
PALACE_STORES_TOTAL = Counter(
    "bgw_palace_stores_total",
    "Memories stored in the palace",
    ["wing", "room"],
)

PALACE_SEARCHES_TOTAL = Counter(
    "bgw_palace_searches_total",
    "Palace search queries",
)

PALACE_SEARCH_LATENCY = Histogram(
    "bgw_palace_search_duration_seconds",
    "Palace search latency (embedding + ChromaDB)",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0],
)

PALACE_MEMORIES_TOTAL = Gauge(
    "bgw_palace_memories_total",
    "Total memories in the palace",
)

# -- Training corpus drain ---------------------------------------------------
TRAINING_CORPUS_RECORDS = Counter(
    "bgw_training_corpus_records_total",
    "New records appended to the training corpus by the nightly drain",
    ["source"],  # owui | state_store | cc_session
)

# -- Expert Model (Qwen3-32B Thinking on Saturn 3090) -----------------------
# Bucket selection tuned to Phase A bench reality: typical thinking turns
# fall in the 30-60s range; hard reasoning runs past 2 minutes; timeout is
# 180s. See `docs/EXPERT_MODEL.md` for the latency discussion.
EXPERT_CALL_COUNT = Counter(
    "bgw_expert_calls_total",
    "Total ask_expert tool invocations",
    ["result"],  # success | error | circuit_open | disabled
)
EXPERT_CALL_LATENCY = Histogram(
    "bgw_expert_call_duration_seconds",
    "ask_expert end-to-end latency (HTTP round-trip, includes thinking time)",
    buckets=[5, 15, 30, 45, 60, 90, 120, 150, 180, 240],
)
EXPERT_REASONING_TOKENS = Histogram(
    "bgw_expert_reasoning_tokens",
    "Reasoning tokens consumed by each ask_expert call (Qwen3 <think> phase)",
    buckets=[100, 500, 1000, 2000, 4000, 8000, 16000, 32000],
)
EXPERT_CIRCUIT_OPEN = Gauge(
    "bgw_expert_circuit_open",
    "1 if the expert circuit breaker is currently open (calls short-circuited)",
)

# -- Voice pipeline latency --------------------------------------------------
# Measures the orchestrator-observable slice of the voice pipeline: from the
# moment HA's conversation agent sends text to /v1/chat/completions to the
# moment we return the final response text. This excludes (a) Whisper STT
# upstream and (b) TTS synthesis + speaker playback downstream. It captures
# the LLM + tool-loop portion — the part we control and the part that's been
# the dominant cost on recent optimizations.
VOICE_PIPELINE_LATENCY = Histogram(
    "bgw_voice_pipeline_seconds",
    "Text-in to text-out latency for voice-channel requests (LLM + tool loop). "
    "Excludes STT upstream and TTS/playback downstream.",
    buckets=[0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 13.0, 17.0, 22.0, 30.0, 45.0, 60.0],
)

# TTS synthesis latency — observed on the /v1/audio/speech proxy in
# routes_vision. Captures time to fetch audio bytes from Qwen3-TTS,
# separate from the orchestrator LLM slice.
VOICE_TTS_LATENCY = Histogram(
    "bgw_voice_tts_seconds",
    "TTS synthesis latency (proxy /v1/audio/speech). Excludes browser playback.",
    buckets=[0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0],
)

# -- Recurring reminders (settings page) -------------------------------------
RECURRING_REMINDERS_EXPANDED = Counter(
    "bgw_recurring_reminders_expanded_total",
    "Number of one-shot reminders materialized from recurring rules.",
)
RECURRING_REMINDERS_EXPAND_ERRORS = Counter(
    "bgw_recurring_reminders_expand_errors_total",
    "Errors raised while expanding due recurring rules into one-shot reminders.",
)

# First-chat welcome: lets us answer "did the dream-install onboarding actually
# fire for the first N users?" — a welcome prepended is end-to-end proof that
# orchestrator+chat+routing all worked on a fresh install.
WELCOME_FIRED = Counter(
    "bgw_welcome_fired_total",
    "First-chat welcome message attempts.",
    ["result"],  # prepended | error
)

# -- Routine scaffolding (F-006) ---------------------------------------------
# Visibility into the morning/evening routine state machine. The auto-ended
# counter is the load-bearing one: a non-skippable step that nudges past its
# cap force-ends the whole routine (the 2026-04-17 evening-meds-stuck-all-night
# class of bug). Alerting on bgw_routine_auto_ended_total lets Grafana page on
# stuck routines instead of relying on the user noticing.
ROUTINE_STARTED = Counter(
    "bgw_routine_started_total",
    "Routine sessions started.",
    ["routine", "triggered_by"],  # triggered_by: user | scheduled
)
ROUTINE_STEPS_ADVANCED = Counter(
    "bgw_routine_steps_advanced_total",
    "Routine steps advanced by an explicit user action.",
    ["routine", "action"],  # action: done | skip
)
ROUTINE_COMPLETED = Counter(
    "bgw_routine_completed_total",
    "Routine sessions that reached the final step.",
    ["routine"],
)
ROUTINE_AUTO_SKIPPED = Counter(
    "bgw_routine_auto_skipped_total",
    "Skippable routine steps auto-skipped after exceeding the nudge cap.",
    ["routine", "step"],
)
ROUTINE_AUTO_ENDED = Counter(
    "bgw_routine_auto_ended_total",
    "Routines force-ended because a step nudged past the cap (non-skippable, "
    "or auto-skip disabled). Stuck-routine signal for alerting.",
    ["routine", "step"],
)

# -- Self-care logging (F-008) -----------------------------------------------
# One increment per logged self-care action, regardless of entry point (the
# selfcare_log tool, the routine bridge, meal_manager, or workout log_set).
SELFCARE_LOGGED = Counter(
    "bgw_selfcare_logged_total",
    "Self-care actions logged.",
    ["action"],  # meal | medication | water | movement
)
