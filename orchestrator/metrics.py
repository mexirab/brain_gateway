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

# Whole chat turn. A real-streamed turn is observed when its SSE stream ends
# (cloud_brain _stream_loop_response), tool rounds included, and counts in
# ACTIVE_REQUESTS until then; before 2026-10-25 both were recorded when the
# StreamingResponse was returned, before the loop ran. A client that hangs up
# mid-reply records the shorter time; one gone before the stream starts
# records nothing, so _count can sit slightly under bgw_requests_total.
# Whole turns mostly take 2-13 s, so the buckets follow the voice-pipeline
# edges there (a 10 -> 30 gap read a 12 s turn as p95 ~28 s); 10 and 30 stay
# edges for the Grafana thresholds and HighRequestLatency.
REQUEST_LATENCY = Histogram(
    "bgw_request_duration_seconds",
    "End-to-end request latency",
    ["mode"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 13.0, 17.0, 22.0, 30.0, 45.0, 60.0, 120.0, 300.0],
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

# How the model's tool calls actually reached us, per model round. This exists
# to measure a KNOWN Qwen3.6 + vLLM defect, not for general curiosity.
#
# The bug: Qwen3.6 sometimes emits <tool_call> INSIDE an unclosed <think> block.
# vLLM's qwen3 reasoning parser then classifies the whole span as reasoning and
# drops the tool call — the API returns populated reasoning_content, an EMPTY
# tool_calls array, and finish_reason="stop" instead of "tool_calls". The
# agentic loop sees "I'll check the calendar" and then simply stops.
# Fixed upstream by vLLM PR #35687 (merged 2026-04-24, first shipped in 0.20.0).
# Helios runs vllm/vllm-openai:v0.19.1 (released 2026-04-18) — SIX DAYS too old.
#
# Labels (`source`):
#   native       - vLLM returned structured tool_calls. The healthy path.
#   dropped      - a <tool_call> marker was present (in content, or in the
#                  reasoning channel) but no call was parsed. The turn ends early
#                  and the user sees the assistant give up mid-task.
#                  *** THIS is the defect signal — watch this label. ***
#   xml_fallback - native tool_calls were empty but parse_xml_tool_calls() still
#                  recovered a call from content. Do NOT expect this to fire for
#                  the bug above, and do not wait for it before upgrading. Two
#                  independent reasons it cannot rescue this defect:
#                    1. The fallback is gated on non-empty `content`
#                       (`if not tool_calls and content:`), but in this failure
#                       the entire span went to the reasoning channel, so
#                       `content` is empty and the fallback never runs.
#                    2. parse_xml_tool_calls() does json.loads() on the tag body,
#                       i.e. it expects <tool_call>{"name":...}</tool_call>.
#                       Helios runs --tool-call-parser qwen3_coder, whose wire
#                       format is <function=name><parameter=x>v</parameter>...
#                       — not JSON. It would raise JSONDecodeError and recover
#                       nothing.
#                  The label is kept because it is a real (if rare) path for
#                  other models/formats, and a nonzero value here would be
#                  genuinely surprising and worth investigating.
#   none         - no tool calls and no marker. Ordinary conversational reply,
#                  the overwhelmingly common case. Tracked as the denominator so
#                  the others are interpretable as a rate rather than a raw count.
#
# NOT every model round is counted, so do not expect this to reconcile against
# bgw_llm_calls_total. Three rounds are deliberately excluded because no
# classification is possible or meaningful: a stream that died mid-emission, a
# call_model that raised, and the forced tools-free "summarize" round taken when
# every tool call in a round is a repeat. All three have their own counters.
TOOL_CALL_SOURCE = Counter(
    "bgw_tool_call_source_total",
    "How tool calls arrived from the model (native vs XML-fallback rescue vs silently dropped vs xml_rejected)",
    ["source"],
)

# Echo-injection defences (2026-10-02 security review): a tool result carried
# <tool_call>/<think>/<|im_start|> markup that was neutralized before reaching
# the model, and a model round that tried to dispatch more than
# MAX_TOOL_CALLS_PER_ROUND calls. Either firing is a signal that untrusted
# content is trying to drive the loop.
TOOL_RESULT_MARKUP_NEUTRALIZED = Counter(
    "bgw_tool_result_markup_neutralized_total",
    "Tool results in which tool-call / chat-template control markup was neutralized",
    ["tool"],
)

TOOL_CALLS_CAPPED = Counter(
    "bgw_tool_calls_capped_total",
    "Model rounds whose tool-call batch exceeded MAX_TOOL_CALLS_PER_ROUND and was truncated",
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

# Spoken vs skipped-for-routine vs suppressed (DND / live voice session) vs
# failed. The morning routine shares the
# 07:00 slot, so "skipped_routine" every day is expected — this exists so a
# briefing that never actually speaks is visible rather than silent.
MORNING_BRIEFING_OUTCOME_TOTAL = Counter(
    "bgw_morning_briefing_outcome_total",
    "Morning briefing outcomes",
    ["outcome"],  # spoken | skipped_routine | suppressed | failed
)
for _outcome in ("spoken", "skipped_routine", "suppressed", "failed"):
    MORNING_BRIEFING_OUTCOME_TOTAL.labels(outcome=_outcome)

# Same dead-man's-switch for the evening shutdown ritual — watched by
# EveningBriefingStale.
EVENING_BRIEFING_LAST_RUN = Gauge(
    "bgw_evening_briefing_last_run_timestamp_seconds",
    "Unix timestamp the evening briefing job last fired",
)

# Evening counterpart of the outcome counter. The evening routine (21:00)
# usually still runs at 21:30, so "skipped_routine" (spoken announce skipped,
# digest mirrored to the inbox/Telegram) is expected on routine nights;
# "skipped_dnd" parks silently with no mirror; "error" = the job crashed
# (the heartbeat still stamps, so this is the only metric that shows it).
EVENING_BRIEFING_OUTCOME_TOTAL = Counter(
    "bgw_evening_briefing_outcome_total",
    "Evening briefing outcomes",
    ["outcome"],  # spoken | skipped_routine | skipped_dnd | suppressed | failed | error
)
for _outcome in ("spoken", "skipped_routine", "skipped_dnd", "suppressed", "failed", "error"):
    EVENING_BRIEFING_OUTCOME_TOTAL.labels(outcome=_outcome)

# Wind-down ladder T-30 nudge heartbeat. Watched by WindDownNudgeStale
# (warning → quiet Pushover, non-paging). The nudge is a spoken prompt, so a
# dropped rung is invisible by omission — and the feature targets exactly the
# person who won't notice the missing 22:00 nudge. Also surfaced in the
# "Sleep Wind-Down" row of the brain-gateway dashboard (dashgen builder).
# These heartbeats (morning/evening/wind-down) are NO LONGER seeded to now() at
# startup: that re-seed masked a genuinely-dead job during active-dev weeks (CI
# recreates the orchestrator on every merge, re-stamping now() so the gauge never
# aged past threshold). Each *Stale alert guards with `... and <gauge> > 0`, so
# the default-0 gauge reads as honest "No data" until the job first fires — no
# pre-first-run false page — and a job that ran then died ages past threshold and
# PUSHES. Residual: a job dead from a fresh boot (gauge never leaves 0) shows
# "No data" but won't page until it runs once; accepted (in-memory gauges reset
# on restart — persisting last-run is a larger change we deliberately skipped).
WIND_DOWN_LAST_RUN = Gauge(
    "bgw_wind_down_last_run_timestamp_seconds",
    "Unix timestamp the wind-down nudge job last fired",
)

# Wind-down ladder T-60 lights-dim heartbeat. Stamped at the TOP of
# wind_down_dim, before its DND / no-scene early returns — so it proves the
# job body ran, independent of whether it attempted a scene (the scene
# counter stays silent on those early returns). No stale alert by design: a
# lights rung that fails to dim IS self-evident in the house, unlike the
# nudge. The dashboard panel is the safety net for a silently-dropped dim job.
WIND_DOWN_DIM_LAST_RUN = Gauge(
    "bgw_wind_down_dim_last_run_timestamp_seconds",
    "Unix timestamp the wind-down lights-dim job last fired",
)

# Per-scene outcomes for the T-60 lights rung — the forensic answer to
# "lights didn't dim last night" (result: ok | failed | error).
WIND_DOWN_SCENE_RESULT = Counter(
    "bgw_wind_down_scene_result_total",
    "Wind-down scene activations by outcome",
    ["scene", "result"],
)

# Time-to-first-token on streamed chat requests — the metric real streaming
# exists to improve. Observed at the first SSE content chunk relayed to the
# client; buffered fallbacks observe at their single final chunk, so a
# streaming regression shows up as this histogram collapsing toward
# REQUEST_LATENCY.
# voice: "true" for streamed voice turns (dashboard mic / call mode — HA Assist
# doesn't stream, so it never lands here). Measured from when the loop starts
# to the first streamed text; the first spoken word also waits for a whole
# sentence and TTS.
CHAT_TTFT = Histogram(
    "bgw_chat_ttft_seconds",
    "Time to first streamed content chunk on /v1/chat/completions",
    ["voice"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0, 55.0],
)
for _voice in ("true", "false"):
    CHAT_TTFT.labels(voice=_voice)

# Streaming degradation events — the happy path (a clean streamed answer) is
# NOT counted; the denominator is bgw_chat_ttft_seconds_count. Only the
# non-ideal outcomes land here so on-call can trend "how often is streaming
# quietly falling back or truncating":
#   not_stream_capable  — the resolved backend can't stream; the whole request
#                         ran buffered (primary should always stream, so this
#                         firing means something is off with it).
#   pre_emission_retry  — a streamed round errored before any token reached the
#                         client; retried buffered (invisible to the user).
#   died_mid_emission   — a stream died AFTER emitting; the user got a SILENTLY
#                         TRUNCATED answer. Watched by ChatStreamTruncating.
#   loop_failed_fallback — the whole loop threw with nothing emitted; fell back
#                          to the fallback model (buffered).
#   loop_failed_error    — the loop threw and the fallback also failed / was
#                          unavailable; the client got the generic apology.
CHAT_STREAM_OUTCOME = Counter(
    "bgw_chat_stream_outcome_total",
    "Streaming chat degradation events (happy path uncounted; denominator = bgw_chat_ttft_seconds_count)",
    ["outcome"],
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

# -- Scheduler reliability ---------------------------------------------------
# APScheduler silently drops a job (apart from its own "Run time ... was missed"
# log warning) when the event loop stalls longer than the job's
# misfire_grace_time (300s scheduler-wide, see shared.py job_defaults). One-shot
# date-trigger jobs (reminder_* delivery, focus-break delivery, dnd_auto_unmute)
# have no next occurrence and no runtime recovery, so a drop is a permanently
# lost action, not a delayed one. Incremented from the EVENT_JOB_MISSED listener
# wired in orchestrator.py startup.
SCHEDULER_JOBS_MISSED = Counter(
    "bgw_scheduler_jobs_missed_total",
    "Scheduler jobs dropped after their misfire_grace_time elapsed (event-loop stall)",
    ["job_family"],
)

# Job-id prefixes that embed a UUID or timestamp — collapsed to a stable family
# label so the counter can't explode Prometheus label cardinality. First
# matching prefix wins. Every other (fixed-string) job id maps to "cron:<id>",
# which is bounded because those ids are literals in the code (one series per
# job type). INVARIANT: when you add a scheduler job whose id embeds a
# UUID/timestamp, add its prefix here — otherwise it lands in cron:<id> and
# blows up cardinality.
_JOB_FAMILY_PREFIXES = (
    ("reminder_", "reminder"),  # reminder_<id>, reminder_<id>_retry
    ("focus_", "focus"),  # focus_<hhmmss>, focus_checkin_*, focus_restored_*
    ("routine_", "routine"),  # routine_<id>, routine_nudge_<hhmmss>
    ("auto_learn_", "auto_learn"),  # auto_learn_<session_key>
    ("interrupt_checkin_", "interrupt"),  # interrupt_checkin_<hhmmss>
    ("ambient_summary_", "ambient"),  # ambient_summary_<...>
)


def scheduler_job_family(job_id: str) -> str:
    """Collapse a raw APScheduler job id to a bounded-cardinality family label.

    Dynamic ids (reminder_<uuid>, focus_<hhmmss>, ...) map to their family;
    fixed-string ids (calendar_poll, morning_briefing, ...) map to cron:<id>.
    """
    for prefix, family in _JOB_FAMILY_PREFIXES:
        if job_id.startswith(prefix):
            return family
    return f"cron:{job_id}"


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
    "Time between reminder trigger_time and user acknowledgement (any channel: ntfy/pushover, telegram, dashboard)",
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

SPREADSHEET_TOTAL = Counter(
    "bgw_spreadsheet_total",
    "Grist calls from the spreadsheet tool",
    # action: list | read | add | update
    # result: ok | partial (list: some docs failed) | write_unknown (write sent,
    #         no confirmation) | disabled | unknown_sheet | bad_table | bad_filter | bad_row_id |
    #         row_not_found | no_values | too_many_rows | unknown_column |
    #         formula_column | bad_value | auth | not_found | http_4xx | http_5xx |
    #         http_other | timeout | connect_error | other
    ["action", "result"],
)

PAPERLESS_READ_TOTAL = Counter(
    "bgw_paperless_read_total",
    "Read-only Paperless-ngx API calls from the paperless_search / paperless_read tools",
    # op: search | get
    # result: ok | not_found | http_4xx | http_5xx | http_other | bad_body |
    #         timeout | connect_error | other | disabled | missing_url |
    #         missing_token
    ["op", "result"],
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

# -- Telegram bot (two-way capture + reminder channel) ------------------------
TELEGRAM_SEND_TOTAL = Counter(
    "bgw_telegram_send_total",
    "Telegram sendMessage attempts",
    # result: ok | fail | skipped
    # kind:   reminder | chat | system
    # reason: ok | http_error | network_error | disabled | missing_token |
    #         missing_chat_id
    ["result", "kind", "reason"],
)

TELEGRAM_SEND_LATENCY = Histogram(
    "bgw_telegram_send_latency_seconds",
    "Latency of Telegram sendMessage calls (all chunks of one reply)",
    ["kind"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)

TELEGRAM_UPDATE_TOTAL = Counter(
    "bgw_telegram_update_total",
    "Inbound Telegram updates by disposition",
    # kind:   message | voice | photo | callback | other
    #         (voice/photo media outcomes are also broken out in TELEGRAM_MEDIA_TOTAL)
    # result: ok | denied | ignored | error
    ["kind", "result"],
)

TELEGRAM_CALLBACK_TOTAL = Counter(
    "bgw_telegram_callback_total",
    "Telegram Done/Snooze button taps",
    # action: ack | snooze | unknown
    # result: ok | already | not_found | limit | error
    ["action", "result"],
)

TELEGRAM_MEDIA_TOTAL = Counter(
    "bgw_telegram_media_total",
    "Inbound Telegram media (voice/photo) processing outcomes",
    # kind:   voice | photo | voice_reply
    # result: ok | disabled | too_long | too_large | download_failed |
    #         stt_unreachable | stt_failed | empty_transcript |
    #         wake_failed | vision_failed |
    #         tts_failed | send_failed   (voice_reply: spoken walkie-talkie reply)
    ["kind", "result"],
)

# Last-persisted getUpdates offset — makes the poll loop's resume-across-restart
# visible on a graph (a reset-to-0 or a stuck value is the failure signal for
# the offset-persistence fix). Set on each _save_offset.
TELEGRAM_UPDATE_OFFSET = Gauge(
    "bgw_telegram_update_offset",
    "Last persisted Telegram getUpdates offset (0 = fresh/no persisted offset)",
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

# Pre-create every series at 0: increase() can't see a counter's first
# increment from "absent", so SelfAuditFailing would miss the first failure
# after each restart, and the series would vanish from Prometheus on restart.
for _result in ("ok", "partial", "failed", "skipped", "busy"):
    SELF_AUDIT_RUNS_TOTAL.labels(result=_result)

# Dead-man's switch for the daily audit, watched by SelfAuditStale. Stamped on
# every completed run (ok/partial/failed) and persisted in app_state, then
# re-seeded at startup from the PERSISTED time — the real last run, not now()
# — so a restart neither masks a dead job nor blanks the panel.
SELF_AUDIT_LAST_RUN = Gauge(
    "bgw_self_audit_last_run_timestamp_seconds",
    "Unix timestamp the self-audit last completed a run (any result but skipped/busy)",
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
    "Audits whose diagnosis is off-format: no **Overall:** line, or its counts disagree "
    "with the severity sections parsed -- the prompt may need tuning",
)

# One Loki query per source group (orchestrator | jupiter_other | helios | ups);
# a group whose sample came back at exactly its line limit was cut off. The
# report still shows the exact count (metric query), but the clusters only
# see the sample.
SELF_AUDIT_TRUNCATED_TOTAL = Counter(
    "bgw_self_audit_truncated_total",
    "Self-audit Loki groups whose sample hit the per-group line limit",
    ["group"],
)
for _group in ("orchestrator", "jupiter_other", "helios", "ups"):
    SELF_AUDIT_TRUNCATED_TOTAL.labels(group=_group)

# 1 = the brain's own journal (llama-server-primary) has no lines in Loki for
# the last 30 min. Idle is normal: HeliosLogsSilent only fires when this is 1
# WHILE llama-server's /metrics shows it served prompts in that window — the
# "Helios is up but its logs stopped reaching Loki" case (dead promtail-helios
# or loki-push-gw). Left unchanged when the Loki check itself fails.
HELIOS_LOGS_SILENT = Gauge(
    "bgw_helios_logs_silent",
    "1 if Loki has no llama-server-primary lines from Helios in the last 30 min",
)
HELIOS_LOG_CHECK_TOTAL = Counter(
    "bgw_helios_log_check_total",
    "Helios log-silence checks against Loki",
    # result: lines | silent | error
    ["result"],
)
for _result in ("lines", "silent", "error"):
    HELIOS_LOG_CHECK_TOTAL.labels(result=_result)

# Read-only investigator (self-monitoring step 2): the brain with
# list_dir/read_file/grep/loki_query tools, run daily and on allow-listed alerts.
SELF_INVESTIGATOR_RUNS_TOTAL = Counter(
    "bgw_self_investigator_runs_total",
    "Self-investigator runs",
    # trigger: daily | alert
    # result:  ok | budget (a limit ran out; final answer forced) |
    #          brain_asleep (skipped, deterministic digest only) | error
    ["trigger", "result"],
)
for _trigger in ("daily", "alert"):
    for _result in ("ok", "budget", "brain_asleep", "error"):
        SELF_INVESTIGATOR_RUNS_TOTAL.labels(trigger=_trigger, result=_result)

SELF_INVESTIGATOR_TOOL_CALLS_TOTAL = Counter(
    "bgw_self_investigator_tool_calls_total",
    "Self-investigator tool calls",
    # tool:   list_dir | read_file | grep | loki_query | unknown
    # result: ok | denied (path/argument refused) | error | duplicate | capped
    ["tool", "result"],
)

SELF_AUDIT_ALERT_TRIGGER_TOTAL = Counter(
    "bgw_self_audit_alert_trigger_total",
    "Alertmanager webhook deliveries to /api/self_audit/alert",
    # result: accepted | ignored (resolved / not allow-listed) | rate_limited |
    #         busy | disabled | bad_request
    ["result"],
)
for _result in ("accepted", "ignored", "rate_limited", "busy", "disabled", "bad_request"):
    SELF_AUDIT_ALERT_TRIGGER_TOTAL.labels(result=_result)

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
    # HELIOS_WAKE_ENABLED or HELIOS_PLUG_MONITOR_ENABLED) plus any on-demand
    # /api/helios/power read. NaN when the power sensor is unavailable/unparseable
    # and before the first read, so it never reports a misleading 0 W (which the
    # Helios alerts would read as "asleep"). A failed HA request leaves it as-is;
    # the alerts catch that via bgw_helios_status_total{result="ok"} going flat.
    "Helios smart-plug power draw in watts (from HA sensor); NaN when unreadable",
)
HELIOS_PLUG_WATTS.set(float("nan"))

EMBEDDING_HEALTHY = Gauge(
    "bgw_embedding_healthy",
    "1 if the embedding model encoded a test sentence at startup, else 0. Every "
    "memory feature (search, RAG context, palace writes, auto_learn) depends on it.",
)

# Runtime counterpart to EMBEDDING_HEALTHY (which only checks once at boot): the
# 2026-10-02 -> 10-15 outage broke every encode() for 12 days with no signal.
# caller = the module that called encode() (bounded: a dozen orchestrator modules).
EMBEDDING_ERRORS = Counter(
    "bgw_embedding_errors_total",
    "Embedding encode() calls that raised, by calling module",
    ["caller"],
)
# Pre-initialised so increase() sees the first failure after a restart.
for _caller in (
    "auto_learn",
    "brain_dump_manager",
    "memory_manager",
    "mempalace",
    "orchestrator",
    "prompt_builder",
    "rag_ingest",
    "routes_documents",
    "tool_handlers",
):
    EMBEDDING_ERRORS.labels(caller=_caller)

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

# Per-source reads by calendar_sources (caldav | exchange | phone | google).
# result: ok | error (unreachable/HTTP error) | stale (phone push too old) |
# corrupt (phone push has records but none parse).
CALENDAR_SOURCE_FETCH = Counter(
    "bgw_calendar_source_fetch_total",
    "Calendar source reads by source and result",
    ["source", "result"],
)

CALENDAR_SOURCE_LATENCY = Histogram(
    "bgw_calendar_source_fetch_duration_seconds",
    "Calendar source read latency",
    ["source"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)

CALENDAR_WRITE = Counter(
    "bgw_calendar_write_total",
    "Calendar event writes by target and result",
    ["target", "result"],
)

PHONE_CALENDAR_LAST_SYNC = Gauge(
    "bgw_phone_calendar_last_sync_timestamp_seconds",
    "Unix time of the last accepted iPhone calendar push (/api/calendar/sync); 0 = never",
)

PHONE_CALENDAR_SYNC_REJECTED = Counter(
    "bgw_phone_calendar_sync_rejected_total",
    "Rejected iPhone calendar pushes by reason",
    ["reason"],
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
# result: ok | no_results (every engine answered, nothing found) | empty (no
# results and SearXNG reported engines suspended / timing out — the search is
# broken) | error | timeout. voice: "true" for voice turns, which get a 5 s
# deadline instead of 15 s. Pre-seeded so increase() sees the first hit
# (WebSearchFailing counts empty|error|timeout).
WEB_SEARCH_COUNT = Counter(
    "bgw_web_search_total",
    "Web searches by outcome and channel",
    ["result", "voice"],
)
for _result in ("ok", "no_results", "empty", "error", "timeout"):
    for _voice in ("true", "false"):
        WEB_SEARCH_COUNT.labels(result=_result, voice=_voice)

# Engines SearXNG listed as unresponsive in a search response (once per engine
# per search). engine = SearXNG engine name ("other" if it isn't label-safe, or
# past 40 distinct names); reason: suspended | timeout | captcha |
# access_denied | too_many_requests | other. Not pre-seeded (engine names are
# only known at runtime), so increase() misses the first hit per engine after
# a restart.
WEB_SEARCH_ENGINE_UNRESPONSIVE = Counter(
    "bgw_web_search_engine_unresponsive_total",
    "SearXNG engines reported unresponsive during a web search",
    ["engine", "reason"],
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

# Facts that passed every filter but were cut by AUTO_LEARN_MAX_FACTS. Steady
# growth means the cap is losing real facts from long conversations.
AUTO_LEARN_FACTS_CAPPED = Counter(
    "bgw_auto_learn_facts_capped_total",
    "Extracted facts dropped by the AUTO_LEARN_MAX_FACTS cap (per extraction window)",
)

# update_memory: older facts replaced by a correction, and replacements it
# held back because the new text wasn't about the same thing.
MEMORY_FACTS_REPLACED = Counter(
    "bgw_memory_facts_replaced_total",
    "Facts deleted by update_memory as superseded by a correction",
)
MEMORY_REPLACE_SKIPPED = Counter(
    "bgw_memory_replace_skipped_total",
    "update_memory search hits kept because the correction text did not match them",
)

AUTO_LEARN_SENSITIVE_FILTERED = Counter(
    "bgw_auto_learn_sensitive_filtered_total",
    "Facts rejected by sensitive data filter",
)

# Facts dropped because their category is a structured domain (meds/health/
# schedule/routine) owned by a YAML source of truth. Auto-learn must NOT shadow
# those — a hallucinated "Naltrexone → morning" once got stored high-confidence
# and served back as fact. See AUTO_LEARN_BLOCKED_CATEGORIES (shared/config).
AUTO_LEARN_CATEGORY_BLOCKED = Counter(
    "bgw_auto_learn_category_blocked_total",
    "Facts rejected because their category is an owned structured domain",
    ["category"],
)

# Pending conversations that were never extracted. reason: evicted (20-entry
# cache full), shutdown (RAM-only cache lost on restart/deploy),
# model_unavailable (brain stayed asleep through every retry).
AUTO_LEARN_DROPPED = Counter(
    "bgw_auto_learn_dropped_total",
    "Pending auto-learn conversations dropped without extraction",
    ["reason"],
)
for _reason in ("evicted", "shutdown", "model_unavailable"):
    AUTO_LEARN_DROPPED.labels(reason=_reason)

# Extraction postponed because the primary model was unreachable at fire time.
AUTO_LEARN_DEFERRED = Counter(
    "bgw_auto_learn_deferred_total",
    "Auto-learn extractions postponed because the model was unreachable",
)

# Failures inside an extraction — without this a failed LLM call or a broken
# store looked exactly like "the conversation had no facts".
AUTO_LEARN_ERRORS = Counter(
    "bgw_auto_learn_errors_total",
    "Auto-learn failures by pipeline stage",
    ["stage"],
)
for _stage in ("llm", "empty", "parse", "dedup", "store", "pipeline"):
    AUTO_LEARN_ERRORS.labels(stage=_stage)

# One per extraction run. Unlabelled-by-category on purpose: facts_stored_total
# creates its category series lazily, so increase() misses the first store per
# category after every restart and can't tell a working day from a broken one.
AUTO_LEARN_RUNS = Counter(
    "bgw_auto_learn_runs_total",
    "Auto-learn extraction runs by outcome",
    ["outcome"],
)
for _outcome in ("stored", "duplicates_only", "no_facts", "opt_out", "error"):
    AUTO_LEARN_RUNS.labels(outcome=_outcome)

# Conversations re-armed because they kept going while their extraction ran
# (APScheduler skipped the re-armed job; see cloud_brain._rearm_if_orphaned).
AUTO_LEARN_REARMED = Counter(
    "bgw_auto_learn_rearmed_total",
    "Auto-learn jobs re-armed after an orphaned snapshot",
)

AUTO_LEARN_EXTRACTION_LATENCY = Histogram(
    "bgw_auto_learn_extraction_duration_seconds",
    "Auto-learn extraction pipeline latency",
    # 120/300: a long conversation is extracted in up to 4 windows (~20 s each).
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
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

# Background / non-loop callers of the primary model (single llama slot, so a
# call can queue behind chat and time out). error_type is the exception class
# name (ReadTimeout, ConnectError, HTTPStatusError, ...) — a small, bounded set.
BACKGROUND_LLM_ERRORS = Counter(
    "bgw_background_llm_errors_total",
    "Failed primary-model calls from background / non-loop callers",
    ["caller", "error_type"],
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

# HA answers play_media with 200 even for an `unavailable` Cast entity, so a
# dead speaker used to log as a successful announcement (the bedroom pair
# went silent for days unnoticed, 2026-09/10). _announce_voice now checks
# entity state first; each skipped target bumps this. Watched by
# AnnouncementSpeakerUnavailable. speaker = entity id when it is a configured
# route/env speaker, else "other" (bounded).
ANNOUNCE_SPEAKER_UNAVAILABLE_TOTAL = Counter(
    "bgw_announcement_speaker_unavailable_total",
    "Announcement targets skipped because HA reports the speaker unavailable/missing",
    ["speaker"],
)

ANNOUNCE_FALLBACK_TOTAL = Counter(
    "bgw_announcement_fallback_total",
    "Announcements re-routed to the reminder speakers because every routed speaker was dead",
    ["type"],
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
    # success | empty | timeout | connect_error | http_error | error | disabled | skipped_unreachable
    # | rejected_mime | rejected_empty | rejected_corrupt | rejected_size
    ["status"],
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
    ["source"],  # state_store | cc_session
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
# path: "streamed" (dashboard mic / call mode, real streaming) is observed
# when the stream ends (cloud_brain _stream_loop_response) — the user hears
# the first sentence long before that; a call hung up mid-reply records the
# shorter time. Before 2026-10-25 streamed turns were recorded before the
# loop ran, so slow tool turns (web_search) never showed. "buffered" (HA
# Assist and any stream=false client, fast path, REAL_STREAMING_ENABLED=false)
# is the whole wait in silence.
VOICE_PIPELINE_LATENCY = Histogram(
    "bgw_voice_pipeline_seconds",
    "Text-in to text-out latency for voice-channel requests (LLM + tool loop). "
    "Excludes STT upstream and TTS/playback downstream.",
    ["path"],
    buckets=[0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 13.0, 17.0, 22.0, 30.0, 45.0, 60.0],
)
for _path in ("streamed", "buffered"):
    VOICE_PIPELINE_LATENCY.labels(path=_path)

# TTS synthesis latency — observed on the /v1/audio/speech proxy in
# routes_vision. Captures time to fetch audio bytes from the TTS server at
# TTS_URL (VoxCPM2 :8004 or Qwen3-TTS :8002), separate from the LLM slice.
VOICE_TTS_LATENCY = Histogram(
    "bgw_voice_tts_seconds",
    "Successful TTS synthesis latency (/v1/audio/speech proxy + dashboard "
    "/api/tts/synthesize, one call per sentence). Excludes browser playback.",
    buckets=[0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 15.0, 20.0],
)

EMPTY_REPLY_TOTAL = Counter(
    "bgw_llm_empty_reply_total",
    "Unified-loop rounds where the model returned no text and no tool call",
    # outcome: retried (first empty round, retried once — thinking on unless already on) |
    #          recovered (the retry produced text) | still_empty (blank reply returned)
    ["outcome"],
)
for _r in ("retried", "recovered", "still_empty"):
    EMPTY_REPLY_TOTAL.labels(outcome=_r)

TTS_STYLE_APPLIED = Counter(
    "bgw_tts_style_applied_total",
    "Style notes resolved for TTS calls (voice_style.py) — one per synthesis call, i.e. per "
    'spoken sentence, NOT per voice_style tool call (see bgw_tool_calls_total{tool="voice_style"})',
    # source: this_reply (one-off "say it angry") | override (ongoing, asked in chat) |
    #         mode (mode_router default) |
    #         announcement (per-type default) | none (no recent chat turn)
    # style: preset key from voice_style.PRESETS ("auto" = no note sent)
    ["source", "style"],
)

DASHBOARD_TTS_TOTAL = Counter(
    "bgw_dashboard_tts_total",
    "Dashboard chat /api/tts/synthesize outcomes (one call per spoken sentence)",
    # result: ok | unreachable (503 — Helios asleep) | timeout (504) |
    #         upstream_error (502) | not_configured | bad_request | too_long
    ["result"],
)
for _r in ("ok", "unreachable", "timeout", "upstream_error", "not_configured", "bad_request", "too_long"):
    DASHBOARD_TTS_TOTAL.labels(result=_r)

DASHBOARD_STT_TOTAL = Counter(
    "bgw_dashboard_stt_total",
    "Dashboard /api/stt/transcribe outcomes (mic button + hands-free call utterances)",
    # result: ok | empty (nothing heard — call mode just keeps listening) |
    #         unreachable (503 — Helios asleep) | timeout (504) |
    #         upstream_error (502) | too_large (413) | not_configured (503)
    ["result"],
)
for _r in ("ok", "empty", "unreachable", "timeout", "upstream_error", "too_large", "not_configured"):
    DASHBOARD_STT_TOTAL.labels(result=_r)

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

# ---------------------------------------------------------------------------
# Budget sync (Actual Budget, replaced YNAB 2026-10) — finance_manager.sync_budget_transactions
# ---------------------------------------------------------------------------

BUDGET_SYNC_TOTAL = Counter(
    "bgw_budget_sync_total",
    "Actual Budget sync attempts by outcome.",
    ["result"],  # ok | error | busy
)
# Pre-create the series so increase()/unless in BudgetSyncFailing see 0, not
# absent, before the first attempt of each label.
for _r in ("ok", "error", "busy"):
    BUDGET_SYNC_TOTAL.labels(result=_r)

BUDGET_SYNC_LAST_SUCCESS = Gauge(
    "bgw_budget_sync_last_success_timestamp_seconds",
    "Unix time of the last successful Actual Budget sync (0 = none since process start).",
)

BUDGET_HISTORY_ROWS = Gauge(
    "bgw_budget_history_rows",
    "Transactions in query_budget's synced 'actual' dataset after the last refresh.",
)

BUDGET_HISTORY_REFRESH_TOTAL = Counter(
    "bgw_budget_history_refresh_total",
    "Refreshes of query_budget's 'actual' dataset after an Actual sync, by result (ok|error).",
    ["result"],
)

# ---------------------------------------------------------------------------
# Workout program builder — workout_manager.generate_workout + the tool handler
# ---------------------------------------------------------------------------

WORKOUT_GENERATED = Counter(
    "bgw_workout_generated_total",
    "Workout generation attempts by outcome.",
    # ok | no_exercises (location gear too thin) | bad_location | ask_location (tool asked where)
    ["result"],
)
for _r in ("ok", "no_exercises", "bad_location", "ask_location"):
    WORKOUT_GENERATED.labels(result=_r)

WORKOUT_PROGRESSION = Counter(
    "bgw_workout_progression_total",
    "Planned exercise targets by progression decision (see workout_manager._plan_target).",
    ["progression"],
)
for _p in ("new", "up", "reps", "hold", "stall_deload", "comeback", "capped", "deload_week"):
    WORKOUT_PROGRESSION.labels(progression=_p)

EXERCISE_INFO_LOOKUPS = Counter(
    "bgw_exercise_info_lookups_total",
    "Exercise how-to lookups (exercise_library.get_info) by outcome.",
    # found | not_found (no faithful library match → video link) | library_missing (not fetched yet)
    ["result"],
)
for _r in ("found", "not_found", "library_missing"):
    EXERCISE_INFO_LOOKUPS.labels(result=_r)

# ---------------------------------------------------------------------------
# Pantry & meal prep (F-015) — pantry_manager
# ---------------------------------------------------------------------------

PANTRY_SCAN = Counter(
    "bgw_pantry_scan_total",
    "Pantry photo scans by kind and outcome.",
    # kind: fridge | pantry | freezer | receipt
    # result: proposed (ready for review) | empty (nothing recognised) | failed
    #         (vision error / brain asleep) | committed | discarded
    ["kind", "result"],
)
for _k in ("fridge", "pantry", "freezer", "receipt"):
    for _r in ("proposed", "empty", "failed", "committed", "discarded"):
        PANTRY_SCAN.labels(kind=_k, result=_r)

PANTRY_RECEIPT_LINES = Counter(
    "bgw_pantry_receipt_lines_total",
    "Receipt lines read, by how they were matched to the pantry.",
    # matched_alias (a learned correction) | matched_inventory | new | non_food
    ["result"],
)
for _r in ("matched_alias", "matched_inventory", "new", "non_food"):
    PANTRY_RECEIPT_LINES.labels(result=_r)

PANTRY_SCAN_LATENCY = Histogram(
    "bgw_pantry_scan_latency_seconds",
    "Wall time to turn one scan's photos into a review proposal (sequential vision calls).",
    # result: proposed | empty | failed — so fast failures don't drag p50 down
    ["kind", "result"],
    buckets=(1, 2.5, 5, 10, 20, 40, 60, 120, 240, 480),
)

PANTRY_EVENTS = Counter(
    "bgw_pantry_events_total",
    "Pantry inventory ledger writes by kind and source.",
    # kind: add | consume | adjust | remove ; source: manual | chat | scan | barcode
    ["kind", "source"],
)
for _k in ("add", "consume", "adjust", "remove"):
    for _s in ("manual", "chat", "scan", "barcode", "receipt", "cook", "decay"):
        PANTRY_EVENTS.labels(kind=_k, source=_s)

BARCODE_LOOKUP = Counter(
    "bgw_barcode_lookup_total",
    "Barcode → product lookups (Open Food Facts) by outcome.",
    # hit (fetched) | cache | miss (unknown product) | error | disabled | invalid
    ["result"],
)
for _r in ("hit", "cache", "miss", "error", "disabled", "invalid"):
    BARCODE_LOOKUP.labels(result=_r)

MEAL_PLAN_GENERATED = Counter(
    "bgw_meal_plan_generated_total",
    "Weekly meal-plan generation attempts by outcome (meal_planner).",
    # ok | retry_ok (first reply unusable, retry with thinking off worked) |
    # invalid (unusable after retry) | llm_error (timeout / bad reply) |
    # asleep (brain unreachable) | error (bug) | interrupted (restart/cancel)
    ["result"],
)
for _r in ("ok", "retry_ok", "invalid", "llm_error", "asleep", "error", "interrupted"):
    MEAL_PLAN_GENERATED.labels(result=_r)

MEAL_PLAN_LATENCY = Histogram(
    "bgw_meal_plan_latency_seconds",
    "Wall time to generate a weekly meal plan (LLM call(s) + validation).",
    buckets=(10, 20, 30, 60, 90, 120, 180, 300),
)

MEAL_PLAN_SWAP = Counter(
    "bgw_meal_plan_swap_total",
    "Single-recipe swaps in a meal plan by outcome.",
    ["result"],  # ok | invalid | llm_error | asleep | busy (another swap running)
)
for _r in ("ok", "invalid", "llm_error", "asleep", "busy"):
    MEAL_PLAN_SWAP.labels(result=_r)

MEAL_PLAN_SWAP_LATENCY = Histogram(
    "bgw_meal_plan_swap_latency_seconds",
    "Wall time of a single-meal swap (LLM call + apply).",
    buckets=(1, 2, 5, 10, 20, 40, 80, 160),
)

# ---------------------------------------------------------------------------
# Pantry consumption tracking (F-015 phase 4) — pantry_consumption
# ---------------------------------------------------------------------------

PANTRY_COOK_EVENTS = Counter(
    "bgw_pantry_cook_events_total",
    "Meals marked cooked (ingredients deducted): planned prep/dinner or an off-plan meal, by where the tap came from.",
    # slot: prep | dinner | other (off-plan) ; source: dashboard | chat | push | undo
    ["slot", "source"],
)
for _sl in ("prep", "dinner", "other"):
    for _so in ("dashboard", "chat", "push", "undo"):
        PANTRY_COOK_EVENTS.labels(slot=_sl, source=_so)

PANTRY_PORTIONS_EATEN = Counter(
    "bgw_pantry_portions_eaten_total",
    "Prepped portions eaten.",
    ["source"],  # dashboard | chat
)
for _so in ("dashboard", "chat"):
    PANTRY_PORTIONS_EATEN.labels(source=_so)

PANTRY_DECAY = Counter(
    "bgw_pantry_decay_total",
    "Daily expiry pass: perishables marked probably_gone and prep batches expired.",
    ["kind"],  # item | batch
)
for _k in ("item", "batch"):
    PANTRY_DECAY.labels(kind=_k)

PANTRY_DECAY_LAST_RUN = Gauge(
    "bgw_pantry_decay_last_run_timestamp_seconds",
    "Unix time the daily pantry expiry pass last completed (re-seeded at startup from app_state).",
)

PANTRY_FRIDGE_CHECK = Counter(
    "bgw_pantry_fridge_check_nudge_total",
    "Weekly fridge-check nudges.",
    ["result"],  # sent | error
)
for _r in ("sent", "error"):
    PANTRY_FRIDGE_CHECK.labels(result=_r)

PANTRY_REMINDER_DONE = Counter(
    "bgw_pantry_reminder_done_total",
    "Done taps on meal-plan cooking reminders (ntfy / Pushover / Telegram).",
    # cooked | already (marked before) | refused (plan replaced) | unlinked (pre-dates phase 4) | error
    ["result"],
)
for _r in ("cooked", "already", "refused", "unlinked", "error"):
    PANTRY_REMINDER_DONE.labels(result=_r)


# -- Home Assistant tool -------------------------------------------------------
# The schema lists entities within a char budget; the model may guess an id.
HA_UNKNOWN_ENTITY = Counter(
    "bgw_ha_unknown_entity_total",
    "home_assistant tool calls refused because the entity_id does not exist (closest matches returned)",
)
# Steerable HA calls refused by ha_guard.check_ha_call (domain, deny list,
# service allow-list, media args). path: tool (chat home_assistant tool),
# nlp (ha_client.execute_command — fast path + /api/ha/command text),
# api (structured /api/ha/command body).
HA_CALL_REFUSED = Counter(
    "bgw_ha_call_refused_total",
    "Home Assistant service calls refused by the shared guard",
    ["path"],
)
for _p in ("tool", "nlp", "api"):
    HA_CALL_REFUSED.labels(path=_p)

# Dashboard /home: HA registry reads (rooms + entity categories) over the WS API.
HA_REGISTRY_FETCH = Counter(
    "bgw_ha_registry_fetch_total",
    "Home Assistant registry fetches for the dashboard home page",
    ["result"],
)
for _r in ("ok", "error"):
    HA_REGISTRY_FETCH.labels(result=_r)
# Entities in the last good registry read; 0 = never read, so the guard's
# device-setting check runs on names only (ha_guard.device_setting_reason).
HA_REGISTRY_ENTITIES = Gauge(
    "bgw_ha_registry_entities",
    "Entities in the last successful Home Assistant registry read (0 = never read)",
)
# Chat / NLP calls refused because the entity is a device setting. source:
# registry (entity_category config/diagnostic), name (id word match), member
# (scene/group containing one).
HA_DEVICE_SETTING_REFUSED = Counter(
    "bgw_ha_device_setting_refused_total",
    "Home Assistant calls refused by the guard as device settings",
    ["path", "source"],
)
for _p in ("tool", "nlp"):
    for _s in ("registry", "name", "member"):
        HA_DEVICE_SETTING_REFUSED.labels(path=_p, source=_s)

# ---------------------------------------------------------------------------
# Document vault — photo name suggestion (routes_documents)
# ---------------------------------------------------------------------------

DOCUMENT_NAME_SUGGEST = Counter(
    "bgw_document_name_suggest_total",
    "Document photo title/category suggestions by outcome.",
    # result: ok | vision_off (disabled or brain unreachable) | timeout |
    #         vision_error (vision server error / image refused) |
    #         unparseable (model reply had no usable JSON/title) |
    #         busy (another photo is being read) | bad_image (not JPEG/PNG/WebP or too big)
    ["result"],
)
for _r in ("ok", "vision_off", "timeout", "vision_error", "unparseable", "busy", "bad_image"):
    DOCUMENT_NAME_SUGGEST.labels(result=_r)

# Dashboard inbox (Done/Snooze cards in /chat — orchestrator/inbox.py).
# kind: reminder | selfcare | message (briefing digests / missed-reminder recap)
INBOX_ITEMS_TOTAL = Counter(
    "bgw_inbox_items_total",
    "Items added to the dashboard inbox",
    ["kind"],
)
# resolution: done | snoozed | dismissed | superseded
# via: dashboard | telegram | ntfy (also Pushover taps — same HMAC route) |
#      logged (selfcare logged another way) | redelivered | renudged |
#      renewed (message card replaced by the next briefing of the same ref)
INBOX_RESOLVED_TOTAL = Counter(
    "bgw_inbox_resolved_total",
    "Dashboard inbox items resolved",
    ["kind", "resolution", "via"],
)
for _k in ("reminder", "selfcare"):
    INBOX_ITEMS_TOTAL.labels(kind=_k)
    INBOX_RESOLVED_TOTAL.labels(kind=_k, resolution="done", via="dashboard")
# kind="message" (briefings / missed-reminder recap) is dismiss-only.
INBOX_ITEMS_TOTAL.labels(kind="message")
INBOX_RESOLVED_TOTAL.labels(kind="message", resolution="dismissed", via="dashboard")
INBOX_RESOLVED_TOTAL.labels(kind="message", resolution="superseded", via="renewed")

# Web Push (orchestrator/webpush_manager.py).
# result: ok | gone (404/410, row deleted) | auth (401/403 — VAPID key changed) |
#         http_4xx | http_5xx | timeout | redirect (refused) | error | no_subscribers
WEBPUSH_SEND_TOTAL = Counter(
    "bgw_webpush_send_total",
    "Web Push sends, one per subscription (no_subscribers = nothing to send to)",
    ["result"],
)
for _r in ("ok", "gone", "auth", "http_4xx", "http_5xx", "timeout", "redirect", "error", "no_subscribers"):
    WEBPUSH_SEND_TOTAL.labels(result=_r)
WEBPUSH_SUBSCRIPTIONS = Gauge(
    "bgw_webpush_subscriptions",
    "Devices currently subscribed to Web Push",
)

# Research helper (orchestrator/research_helper.py, `look_it_up`) — default off.
# result: ok | retry_ok (empty first reply, the no-tools retry answered) | budget (answered
#         after the time/tool budget ran out) | no_answer | brain_error | timeout (outer cap) |
#         cancelled (chat client went away) | error | busy | rate_limited | bad_request |
#         disabled | screened_<secret|contact|private_host|private_address|path|personal>
RESEARCH_LOOKUPS_TOTAL = Counter(
    "bgw_research_lookups_total",
    "look_it_up research-helper lookups",
    ["topic", "result"],
)
# tool: web_search | read_page | library_docs | unknown
# result: ok | empty | refused (read_page: non-public address, bad URL, non-text, HTTP error,
#         bad encoding) | not_from_results (read_page: URL not from this lookup's results) |
#         error | timeout | rate_limited | denied | duplicate | capped
RESEARCH_LOOKUP_DURATION = Histogram(
    "bgw_research_lookup_duration_seconds",
    "Wall time of a look_it_up lookup (the chat turn waits on it)",
    ["topic"],
    buckets=(5, 10, 20, 30, 45, 60, 75, 90, 105, 120),
)
RESEARCH_SOURCE_CALLS_TOTAL = Counter(
    "bgw_research_source_calls_total",
    "Source-tool calls made by the research helper",
    ["tool", "result"],
)
