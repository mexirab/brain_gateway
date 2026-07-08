"""
brain-gateway (uid brain-gateway-overview) — full application telemetry.

The single canonical Brain Gateway app dashboard. Consolidates the former
hand-maintained "Overview" and "Deep Dive" dashboards into this generated one.
Covers every metric defined in orchestrator/metrics.py, grouped by subsystem
into collapsible rows. Infra (host CPU/mem/GPU/disk) lives in the separate
Homelab Infrastructure dashboard — intentionally none of that here. Use when
Jess Glance shows a symptom but you need to drill into which subsystem is
misbehaving.

Template variables:
  $tool        — filter tool-call panels to one tool
  $mode        — filter request panels to one routing mode
  $speaker     — filter TTS panels to one target speaker
  $wing        — filter palace panels to one memory wing
  $request_id  — paste a request_id to trace a single request in logs

Layout: rows are expanded by default. Grafana users can collapse whichever
row they don't need. Each row is 24 columns wide with panels split evenly.
"""

from __future__ import annotations

from ..dashboard import dashboard, var_query, var_text
from ..layout import assign_ids, grid_row, row_divider
from ..panels import (
    EXPERT_CIRCUIT_THRESHOLDS,
    EXPERT_LATENCY_THRESHOLDS_S,
    GREEN_RED_BINARY,
    LATENCY_THRESHOLDS_S,
    VOICE_LATENCY_THRESHOLDS_S,
    VOICE_TTS_THRESHOLDS_S,
    gauge,
    logs,
    piechart,
    stat,
    timeseries,
)


def build() -> dict:
    panels: list[dict] = []
    y = 0

    # -------------------------------------------------------- Request Flow
    r, y = row_divider("Request Flow", y)
    panels.append(r)

    request_flow = [
        timeseries(
            "Request Rate by Mode",
            [('sum by (mode) (rate(bgw_requests_total{mode=~"$mode"}[5m])) * 60', "{{mode}}")],
            unit="none",
            stack=True,
            fill=40,
        ),
        timeseries(
            "Request Latency (p50 / p95 / p99) by Mode",
            [
                (
                    'histogram_quantile(0.50, sum by (le, mode) (rate(bgw_request_duration_seconds_bucket{mode=~"$mode"}[5m])))',
                    "p50 {{mode}}",
                ),
                (
                    'histogram_quantile(0.95, sum by (le, mode) (rate(bgw_request_duration_seconds_bucket{mode=~"$mode"}[5m])))',
                    "p95 {{mode}}",
                ),
                (
                    'histogram_quantile(0.99, sum by (le, mode) (rate(bgw_request_duration_seconds_bucket{mode=~"$mode"}[5m])))',
                    "p99 {{mode}}",
                ),
            ],
            unit="s",
        ),
        timeseries(
            "Request Errors by Type",
            [("sum by (error_type) (rate(bgw_request_errors_total[5m])) * 60", "{{error_type}}")],
            unit="none",
            stack=True,
            fill=60,
        ),
        timeseries(
            "Active Requests + Tool Loop Rounds",
            [
                ("bgw_active_requests", "active requests"),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_tool_loop_rounds_bucket[10m])))",
                    "p95 loop rounds",
                ),
            ],
            unit="none",
        ),
    ]
    row, y = grid_row(request_flow, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # Real streaming (SSE token relay through the unified tool loop): TTFT is the
    # metric the feature exists to improve; the outcome counter surfaces silent
    # degradation (buffered fallback, mid-emission truncation).
    streaming_flow = [
        timeseries(
            "Chat TTFT (p50 / p95 / p99)",
            [
                ("histogram_quantile(0.50, sum by (le) (rate(bgw_chat_ttft_seconds_bucket[5m])))", "p50"),
                ("histogram_quantile(0.95, sum by (le) (rate(bgw_chat_ttft_seconds_bucket[5m])))", "p95"),
                ("histogram_quantile(0.99, sum by (le) (rate(bgw_chat_ttft_seconds_bucket[5m])))", "p99"),
            ],
            unit="s",
            description="Time to first streamed token on /v1/chat/completions. If this "
            "collapses toward Request Latency, streaming has silently degraded to the "
            "buffered path — cross-check the degradation panel.",
        ),
        timeseries(
            "Stream Degradation Events (/min)",
            [("sum by (outcome) (rate(bgw_chat_stream_outcome_total[5m])) * 60", "{{outcome}}")],
            unit="none",
            stack=True,
            description="Non-happy streaming outcomes (happy path is uncounted; denominator "
            "is the TTFT count). died_mid_emission = the user got a truncated answer.",
        ),
        stat(
            "Truncated Answers (died_mid_emission, 24h)",
            'sum(increase(bgw_chat_stream_outcome_total{outcome="died_mid_emission"}[24h])) or vector(0)',
            unit="none",
            thresholds=[(None, "green"), (1, "orange")],
            description="Streams that died after emitting → user saw a silently truncated "
            "reply. Watched by the ChatStreamTruncating alert.",
        ),
    ]
    row, y = grid_row(streaming_flow, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Mode Router
    r, y = row_divider("Mode Router", y)
    panels.append(r)

    mode_router_row = [
        timeseries(
            "Mode Routing (1h)",
            [("sum by (mode) (increase(bgw_mode_route_total[1h]))", "{{mode}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Intensity (1h)",
            [("sum by (intensity) (increase(bgw_mode_route_total[1h]))", "{{intensity}}")],
            unit="none",
            stack=True,
        ),
    ]
    row, y = grid_row(mode_router_row, y, heights=[8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Voice Pipeline
    r, y = row_divider("Voice Pipeline", y)
    panels.append(r)

    voice_desc = (
        "Text-in to text-out latency for voice-channel requests. Covers the "
        "LLM + tool-loop slice we control; excludes Whisper STT upstream and "
        "TTS synthesis + speaker playback downstream. Target: keep p95 under "
        "13s (prior floor). Investigate spikes with the logs row below."
    )
    voice_row = [
        timeseries(
            "Voice Pipeline Latency (p50 / p95 / p99)",
            [
                (
                    "histogram_quantile(0.50, sum by (le) (rate(bgw_voice_pipeline_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_voice_pipeline_seconds_bucket[5m])))",
                    "p95",
                ),
                (
                    "histogram_quantile(0.99, sum by (le) (rate(bgw_voice_pipeline_seconds_bucket[5m])))",
                    "p99",
                ),
            ],
            unit="s",
            thresholds=VOICE_LATENCY_THRESHOLDS_S,
            description=voice_desc,
        ),
        timeseries(
            "Voice Request Rate",
            [("sum(rate(bgw_voice_pipeline_seconds_count[5m])) * 60", "voice turns/min")],
            unit="none",
        ),
        stat(
            "Voice Pipeline Mean (5m)",
            "(sum(rate(bgw_voice_pipeline_seconds_sum[5m])) "
            "/ sum(rate(bgw_voice_pipeline_seconds_count[5m]))) "
            "or on() vector(0)",
            unit="s",
            decimals=1,
            thresholds=VOICE_LATENCY_THRESHOLDS_S,
        ),
        stat(
            "Voice Turns (24h)",
            "sum(increase(bgw_voice_pipeline_seconds_count[24h]))",
            unit="none",
        ),
    ]
    row, y = grid_row(voice_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- TTS Synthesis
    tts_desc = (
        "TTS round-trip at the orchestrator's /v1/audio/speech proxy (the hop "
        "between OWUI and Qwen3-TTS on port 8002). Excludes browser playback. "
        "The proxy also prepends ~150ms of silence so the first-word stutter "
        "from cold prosody state doesn't reach the user — see TTS_SILENCE_PAD_MS."
    )
    tts_row = [
        timeseries(
            "TTS Latency (p50 / p95 / p99)",
            [
                (
                    "histogram_quantile(0.50, sum by (le) (rate(bgw_voice_tts_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_voice_tts_seconds_bucket[5m])))",
                    "p95",
                ),
                (
                    "histogram_quantile(0.99, sum by (le) (rate(bgw_voice_tts_seconds_bucket[5m])))",
                    "p99",
                ),
            ],
            unit="s",
            thresholds=VOICE_TTS_THRESHOLDS_S,
            description=tts_desc,
        ),
        timeseries(
            "TTS Call Rate",
            [("sum(rate(bgw_voice_tts_seconds_count[5m])) * 60", "tts calls/min")],
            unit="none",
        ),
        stat(
            "TTS Mean (5m)",
            "(sum(rate(bgw_voice_tts_seconds_sum[5m])) / sum(rate(bgw_voice_tts_seconds_count[5m]))) or on() vector(0)",
            unit="s",
            decimals=2,
            thresholds=VOICE_TTS_THRESHOLDS_S,
        ),
        stat(
            "TTS Calls (24h)",
            "sum(increase(bgw_voice_tts_seconds_count[24h]))",
            unit="none",
        ),
    ]
    row, y = grid_row(tts_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- LLM Performance
    r, y = row_divider("LLM Performance", y)
    panels.append(r)

    llm_row = [
        timeseries(
            "LLM Calls by Model + Purpose",
            [("sum by (model, purpose) (rate(bgw_llm_calls_total[5m])) * 60", "{{model}} / {{purpose}}")],
            unit="none",
            stack=True,
            fill=40,
        ),
        timeseries(
            "LLM Latency p95 by Purpose",
            [
                (
                    "histogram_quantile(0.95, sum by (le, purpose) (rate(bgw_llm_call_duration_seconds_bucket[5m])))",
                    "{{purpose}}",
                )
            ],
            unit="s",
            thresholds=LATENCY_THRESHOLDS_S,
        ),
        timeseries(
            "LLM Errors by Type",
            [("sum by (model, error_type) (rate(bgw_llm_call_errors_total[5m])) * 60", "{{model}} / {{error_type}}")],
            unit="none",
            stack=True,
        ),
        stat(
            "Primary / Fallback Online",
            "bgw_helios_online + (2 * bgw_fallback_online)",
            text_mode="value_and_name",
            color_mode="background",
            graph_mode="none",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "BOTH DOWN", "color": "red", "index": 0},
                        "1": {"text": "PRIMARY ONLY", "color": "green", "index": 1},
                        "2": {"text": "FALLBACK ONLY", "color": "yellow", "index": 2},
                        "3": {"text": "BOTH UP", "color": "green", "index": 3},
                    },
                }
            ],
            thresholds=GREEN_RED_BINARY,
        ),
    ]
    row, y = grid_row(llm_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Helios Power Management
    r, y = row_divider("Helios Power Management", y)
    panels.append(r)

    helios_row = [
        stat(
            "Helios Running",
            "bgw_helios_running",
            text_mode="value_and_name",
            color_mode="background",
            graph_mode="none",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "OFF", "color": "red", "index": 0},
                        "1": {"text": "ON", "color": "green", "index": 1},
                    },
                }
            ],
            thresholds=GREEN_RED_BINARY,
        ),
        stat(
            "Helios Plug Power",
            "bgw_helios_plug_watts",
            unit="watt",
        ),
        timeseries(
            "Model Server Start/Stop (1h)",
            [
                ("increase(bgw_model_server_starts_total[1h])", "starts"),
                ("increase(bgw_model_server_stops_total[1h])", "stops"),
            ],
            unit="none",
        ),
        timeseries(
            "code_agent Preflight vs Helios (1h)",
            [
                ("increase(bgw_code_agent_preflight_failures_total[1h])", "preflight skips"),
                ("bgw_helios_running", "helios running (0/1)"),
            ],
            unit="none",
            description=(
                "code_agent runs skipped because the code model endpoint was unreachable "
                "at preflight. Skips while 'helios running' is 0 are EXPECTED (Helios asleep). "
                "Skips while it is 1 mean Helios is powered but the code model endpoint is "
                "dead — the user then gets a misleading 'wake Helios' message and it needs "
                "investigation."
            ),
        ),
    ]
    row, y = grid_row(helios_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Expert Agent
    r, y = row_divider("Expert Agent (Saturn 3090 Qwen3-32B)", y)
    panels.append(r)

    expert_desc = (
        "ask_expert tool delegates hard reasoning to Qwen3-32B Thinking on "
        "Saturn 3090 (10.0.0.58:8084). 30-150s latency is normal for full "
        "reasoning passes. Circuit breaker opens after N consecutive failures "
        "(EXPERT_CIRCUIT_BREAKER_FAILURES, default 3); the stat goes red."
    )
    expert_row = [
        stat(
            "Circuit Breaker",
            "bgw_expert_circuit_open",
            text_mode="value_and_name",
            color_mode="background",
            graph_mode="none",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "CLOSED", "color": "green", "index": 0},
                        "1": {"text": "OPEN", "color": "red", "index": 1},
                    },
                }
            ],
            thresholds=EXPERT_CIRCUIT_THRESHOLDS,
        ),
        timeseries(
            "Expert Calls by Result",
            [
                (
                    "sum by (result) (rate(bgw_expert_call_count_total[5m])) * 60",
                    "{{result}}",
                )
            ],
            unit="none",
            stack=True,
            fill=40,
            description=expert_desc,
        ),
        timeseries(
            "Expert Latency (p50 / p95 / p99)",
            [
                (
                    "histogram_quantile(0.50, sum by (le) (rate(bgw_expert_call_latency_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_expert_call_latency_seconds_bucket[5m])))",
                    "p95",
                ),
                (
                    "histogram_quantile(0.99, sum by (le) (rate(bgw_expert_call_latency_seconds_bucket[5m])))",
                    "p99",
                ),
            ],
            unit="s",
            thresholds=EXPERT_LATENCY_THRESHOLDS_S,
        ),
        stat(
            "Expert Reasoning Tokens (mean)",
            "(sum(rate(bgw_expert_reasoning_tokens_sum[5m])) "
            "/ sum(rate(bgw_expert_reasoning_tokens_count[5m]))) "
            "or on() vector(0)",
            unit="none",
            decimals=0,
        ),
    ]
    row, y = grid_row(expert_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Tools
    r, y = row_divider("Tools", y)
    panels.append(r)

    tools_row = [
        timeseries(
            "Tool Call Rate (filtered by $tool)",
            [('sum by (tool) (rate(bgw_tool_calls_total{tool=~"$tool"}[5m])) * 60', "{{tool}}")],
            unit="none",
            stack=True,
            fill=60,
        ),
        timeseries(
            "Tool Latency p95 (filtered by $tool)",
            [
                (
                    'histogram_quantile(0.95, sum by (le, tool) (rate(bgw_tool_call_duration_seconds_bucket{tool=~"$tool"}[5m])))',
                    "{{tool}}",
                )
            ],
            unit="s",
            thresholds=LATENCY_THRESHOLDS_S,
        ),
        timeseries(
            "Tool Errors (filtered by $tool)",
            [('sum by (tool) (rate(bgw_tool_call_errors_total{tool=~"$tool"}[5m])) * 60', "{{tool}}")],
            unit="none",
            stack=True,
        ),
    ]
    row, y = grid_row(tools_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Memory (Palace + RAG)
    r, y = row_divider("Memory (MemPalace + RAG)", y)
    panels.append(r)

    memory_row = [
        timeseries(
            "Palace Stores by Wing (filtered by $wing)",
            [('sum by (wing) (rate(bgw_palace_stores_total{wing=~"$wing"}[5m])) * 60', "{{wing}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Palace Search Latency p50/p95",
            [
                (
                    "histogram_quantile(0.50, sum by (le) (rate(bgw_palace_search_duration_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_palace_search_duration_seconds_bucket[5m])))",
                    "p95",
                ),
            ],
            unit="s",
        ),
        stat(
            "Total Memories",
            "bgw_palace_memories_total",
            unit="none",
            graph_mode="area",
        ),
        timeseries(
            "RAG Query Latency + Results/Query",
            [
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_rag_query_duration_seconds_bucket[5m])))",
                    "p95 latency",
                ),
                (
                    "histogram_quantile(0.5, sum by (le) (rate(bgw_rag_results_count_bucket[5m])))",
                    "p50 results",
                ),
            ],
            unit="s",
        ),
    ]
    row, y = grid_row(memory_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Auto-Learn
    r, y = row_divider("Auto-Learn", y)
    panels.append(r)

    autolearn_row = [
        timeseries(
            "Extractions + Facts Stored",
            [
                ("rate(bgw_auto_learn_extractions_total[15m]) * 60", "extractions/min"),
                ("sum by (category) (rate(bgw_auto_learn_facts_stored_total[15m])) * 60", "stored {{category}}"),
            ],
            unit="none",
        ),
        timeseries(
            "Dedup + Sensitive Filtered",
            [
                ("rate(bgw_auto_learn_duplicates_skipped_total[15m]) * 60", "dedup skipped"),
                ("rate(bgw_auto_learn_sensitive_filtered_total[15m]) * 60", "sensitive filtered"),
            ],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Extraction Latency p95",
            [
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_auto_learn_extraction_duration_seconds_bucket[15m])))",
                    "p95",
                )
            ],
            unit="s",
            thresholds=LATENCY_THRESHOLDS_S,
        ),
    ]
    row, y = grid_row(autolearn_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Brain Dump
    r, y = row_divider("Brain Dump", y)
    panels.append(r)

    braindump_row = [
        timeseries(
            "Items Captured by Category",
            [("sum by (category) (rate(bgw_brain_dump_items_captured_total[15m])) * 60", "{{category}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Items Routed by Destination",
            [("sum by (destination) (rate(bgw_brain_dump_items_routed_total[15m])) * 60", "{{destination}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "RAG Upsert Latency p95 + Errors",
            [
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_brain_dump_rag_duration_seconds_bucket[15m])))",
                    "p95 latency",
                ),
                ("sum by (operation) (rate(bgw_brain_dump_errors_total[15m])) * 60", "errors {{operation}}"),
            ],
            unit="s",
        ),
    ]
    row, y = grid_row(braindump_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Task Decomposition
    r, y = row_divider("Task Decomposition", y)
    panels.append(r)

    taskdecomp_row = [
        timeseries(
            "Task Lifecycle",
            [
                ("rate(bgw_task_decomp_tasks_created_total[15m]) * 60", "created"),
                ("rate(bgw_task_decomp_tasks_abandoned_total[15m]) * 60", "abandoned"),
            ],
            unit="none",
        ),
        timeseries(
            "Step Outcomes",
            [
                ("rate(bgw_task_decomp_steps_completed_total[15m]) * 60", "completed"),
                ("rate(bgw_task_decomp_steps_skipped_total[15m]) * 60", "skipped"),
            ],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Errors",
            [("rate(bgw_task_decomp_errors_total[15m]) * 60", "errors/min")],
            unit="none",
        ),
    ]
    row, y = grid_row(taskdecomp_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Task Backlog
    r, y = row_divider("Task Backlog", y)
    panels.append(r)

    backlog_desc = (
        "The durable to-do list (state_store tasks table). Distinct from Task "
        "Decomposition above (ephemeral step-tracking) and Reminders (time-based). "
        "'Open Tasks' is a live gauge, primed on startup and updated on every "
        "add/complete/drop. A steadily climbing open count with little completion "
        "is the backlog-graveyard smell the Sunday review job is meant to catch."
    )
    backlog_row = [
        stat(
            "Open Tasks",
            "bgw_tasks_open",
            unit="none",
            graph_mode="area",
            thresholds=[(None, "green"), (15, "yellow"), (30, "red")],
        ),
        timeseries(
            "Task Flow (created / completed / dropped)",
            [
                ("sum(rate(bgw_tasks_created_total[1h])) * 3600", "created/hr"),
                ("rate(bgw_tasks_completed_total[1h]) * 3600", "completed/hr"),
                ("rate(bgw_tasks_dropped_total[1h]) * 3600", "dropped/hr"),
            ],
            unit="none",
            description=backlog_desc,
        ),
        timeseries(
            "Tasks Created by Source (24h)",
            [("sum by (source) (increase(bgw_tasks_created_total[24h]))", "{{source}}")],
            unit="none",
            stack=True,
            fill=40,
        ),
    ]
    row, y = grid_row(backlog_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Progress Tracking
    r, y = row_divider("Progress Tracking", y)
    panels.append(r)

    progress_row = [
        timeseries(
            "Events by Type",
            [("sum by (event_type) (rate(bgw_progress_events_total[15m])) * 60", "{{event_type}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Streak Milestones by Category",
            [("sum by (category) (rate(bgw_progress_streak_milestones_total[1h])) * 3600", "{{category}}")],
            unit="none",
        ),
    ]
    row, y = grid_row(progress_row, y, heights=[8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Focus Timer
    r, y = row_divider("Focus Timer", y)
    panels.append(r)

    focus_row = [
        timeseries(
            "Starts by Soundscape + Active",
            [
                ("sum by (soundscape) (rate(bgw_focus_sessions_started_total[1h])) * 3600", "started {{soundscape}}"),
                ("bgw_focus_active", "active"),
            ],
            unit="none",
        ),
        gauge(
            "Completion Rate (24h)",
            "(sum(increase(bgw_focus_sessions_completed_total[24h])) / clamp_min(sum(increase(bgw_focus_sessions_started_total[24h])), 1)) * 100",
            unit="percent",
            thresholds=[(None, "red"), (40, "yellow"), (70, "green")],
        ),
        timeseries(
            "Session Duration Distribution",
            [
                (
                    "histogram_quantile(0.5, sum by (le) (rate(bgw_focus_session_actual_minutes_bucket[24h])))",
                    "p50 min",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_focus_session_actual_minutes_bucket[24h])))",
                    "p95 min",
                ),
            ],
            unit="m",
        ),
        timeseries(
            "Pi-hole Blocking Toggles",
            [("sum by (action) (rate(bgw_pihole_blocking_toggles_total[1h])) * 3600", "{{action}}")],
            unit="none",
            stack=True,
        ),
    ]
    row, y = grid_row(focus_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Calendar + Gmail + Email→Cal
    r, y = row_divider("Calendar + Gmail + Email→Calendar", y)
    panels.append(r)

    calgmail_row_1 = [
        timeseries(
            "Calendar API (calls + errors by operation)",
            [
                ("sum by (operation) (rate(bgw_calendar_api_calls_total[15m])) * 60", "{{operation}}"),
                ("sum by (operation) (rate(bgw_calendar_api_errors_total[15m])) * 60", "err {{operation}}"),
            ],
            unit="none",
        ),
        timeseries(
            "Calendar API Latency p95 by operation",
            [
                (
                    "histogram_quantile(0.95, sum by (le, operation) (rate(bgw_calendar_api_duration_seconds_bucket[15m])))",
                    "{{operation}}",
                )
            ],
            unit="s",
        ),
        timeseries(
            "Gmail API (calls + errors by operation)",
            [
                ("sum by (operation) (rate(bgw_gmail_api_calls_total[15m])) * 60", "{{operation}}"),
                ("sum by (operation) (rate(bgw_gmail_api_errors_total[15m])) * 60", "err {{operation}}"),
            ],
            unit="none",
        ),
    ]
    row, y = grid_row(calgmail_row_1, y, heights=[8, 8, 8])
    panels.extend(row)

    calgmail_row_2 = [
        timeseries(
            "Calendar Poll Events Announced",
            [("rate(bgw_calendar_poll_events_announced_total[1h]) * 3600", "events/hr")],
            unit="none",
        ),
        timeseries(
            "Email → Calendar",
            [
                ("rate(bgw_email_to_calendar_emails_scanned_total[1h]) * 3600", "scanned/hr"),
                ("rate(bgw_email_to_calendar_events_created_total[1h]) * 3600", "created/hr"),
            ],
            unit="none",
        ),
    ]
    row, y = grid_row(calgmail_row_2, y, heights=[8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Vision + Web Search + TTS
    r, y = row_divider("Vision + Web Search + TTS", y)
    panels.append(r)

    vwstts_row_1 = [
        timeseries(
            "Vision Requests by Status",
            [("sum by (status) (rate(bgw_vision_requests_total[15m])) * 60", "{{status}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Vision Latency p95 + Image Size p95",
            [
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_vision_request_duration_seconds_bucket[15m])))",
                    "p95 latency (s)",
                ),
            ],
            unit="s",
        ),
        timeseries(
            "Web Search Rate + Latency",
            [
                ("rate(bgw_web_search_total[15m]) * 60", "searches/min"),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_web_search_duration_seconds_bucket[15m])))",
                    "p95 latency (s)",
                ),
            ],
            unit="none",
        ),
    ]
    row, y = grid_row(vwstts_row_1, y, heights=[8, 8, 8])
    panels.extend(row)

    vwstts_row_2 = [
        timeseries(
            "TTS Announcements (filtered by $speaker)",
            [
                (
                    'sum by (type, speaker) (rate(bgw_tts_announcements_total{speaker=~"$speaker"}[15m])) * 60',
                    "{{type}} → {{speaker}}",
                )
            ],
            unit="none",
            stack=True,
        ),
        timeseries(
            "TTS Latency p50/p95/p99",
            [
                ("histogram_quantile(0.50, sum by (le) (rate(bgw_tts_latency_seconds_bucket[15m])))", "p50"),
                ("histogram_quantile(0.95, sum by (le) (rate(bgw_tts_latency_seconds_bucket[15m])))", "p95"),
                ("histogram_quantile(0.99, sum by (le) (rate(bgw_tts_latency_seconds_bucket[15m])))", "p99"),
            ],
            unit="s",
        ),
        piechart(
            "TTS Errors by Type (24h)",
            "sum by (error_type) (increase(bgw_tts_errors_total[24h]))",
        ),
    ]
    row, y = grid_row(vwstts_row_2, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Reminders + Fast Path
    r, y = row_divider("Reminders + Fast Path", y)
    panels.append(r)

    reminders_row = [
        timeseries(
            "Reminders Set by Target",
            [("sum by (target) (rate(bgw_reminders_set_total[1h])) * 3600", "set {{target}}")],
            unit="none",
            stack=True,
        ),
        timeseries(
            "Reminders Delivered vs Pending",
            [
                ("rate(bgw_reminders_delivered_total[1h]) * 3600", "delivered/hr"),
                ("bgw_reminders_pending", "pending"),
            ],
            unit="none",
        ),
        timeseries(
            "Fast Path Usage",
            [
                ("sum by (action) (rate(bgw_fast_path_total[15m])) * 60", "fast {{action}}"),
                ("rate(bgw_fast_path_bypass_total[15m]) * 60", "bypass → LLM"),
            ],
            unit="none",
            stack=True,
        ),
    ]
    row, y = grid_row(reminders_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Reminder Delivery — Trust
    # The trust layer: is delivery actually reaching the user, and if not,
    # which channel is lying? Failed/missed counters come from the PR #32
    # state machine; per-channel push results from ntfy (F-011), Pushover
    # (F-013), and Telegram; TTS per-speaker success from announcement
    # observability.
    r, y = row_divider("Reminder Delivery — Trust", y)
    panels.append(r)

    trust_outcomes_row = [
        timeseries(
            "Delivery Outcomes (/day)",
            [
                ("sum(increase(bgw_reminders_delivered_total[1d]))", "delivered"),
                ("sum(increase(bgw_reminders_failed_total[1d]))", "failed"),
                ("sum(increase(bgw_reminders_missed_total[1d]))", "missed"),
            ],
            unit="none",
            description="Rolling 1-day totals. Failed = every channel exhausted; missed = found >24h past due at startup.",
        ),
        stat(
            "Failed (7d)",
            "sum(increase(bgw_reminders_failed_total[7d])) or vector(0)",
            unit="none",
            thresholds=[(0, "green"), (1, "red")],
        ),
        stat(
            "Missed (7d)",
            "sum(increase(bgw_reminders_missed_total[7d])) or vector(0)",
            unit="none",
            thresholds=[(0, "green"), (1, "orange")],
        ),
        timeseries(
            "Ack Latency (trigger → Done tap)",
            [
                (
                    "histogram_quantile(0.50, sum(rate(bgw_reminder_ack_latency_seconds_bucket[6h])) by (le))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum(rate(bgw_reminder_ack_latency_seconds_bucket[6h])) by (le))",
                    "p95",
                ),
            ],
            unit="s",
        ),
    ]
    row, y = grid_row(trust_outcomes_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    trust_channels_row = [
        timeseries(
            "Push Channels — OK (/h)",
            [
                ('sum(rate(bgw_ntfy_push_total{result="ok",kind="reminder"}[1h])) * 3600', "ntfy"),
                ('sum(rate(bgw_pushover_push_total{result="ok",kind="reminder"}[1h])) * 3600', "pushover"),
                ('sum(rate(bgw_telegram_send_total{result="ok",kind="reminder"}[1h])) * 3600', "telegram"),
            ],
            unit="none",
        ),
        timeseries(
            "Push Channels — Failures (/h)",
            [
                ('sum(rate(bgw_ntfy_push_total{result="fail"}[1h])) * 3600', "ntfy fail"),
                ('sum(rate(bgw_pushover_push_total{result="fail"}[1h])) * 3600', "pushover fail"),
                ('sum(rate(bgw_telegram_send_total{result="fail"}[1h])) * 3600', "telegram fail"),
            ],
            unit="none",
            thresholds=[(0, "green"), (1, "red")],
        ),
        timeseries(
            "TTS by Speaker (success vs fail, /h)",
            [
                (
                    'sum by (speaker) (rate(bgw_tts_announcements_total{success="true"}[1h])) * 3600',
                    "ok {{speaker}}",
                ),
                (
                    'sum by (speaker) (rate(bgw_tts_announcements_total{success="false"}[1h])) * 3600',
                    "FAIL {{speaker}}",
                ),
            ],
            unit="none",
            description="Per-speaker announcement health — a failing speaker group shows up here before anyone notices missed audio.",
        ),
    ]
    row, y = grid_row(trust_channels_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Telegram Bot
    # The away-from-home surface: inbound updates, media (voice→STT / photo→vision
    # / spoken walkie-talkie reply), and Done/Snooze button taps. The callback
    # panel is the forensic view for "I tapped Done but kept getting nagged" — a
    # rising ack{result=not_found} or a flat callback rate during active reminders
    # is the signal. Media failures while Helios sleeps (stt_unreachable) are
    # expected; tts_failed/vision_failed are the actionable ones.
    r, y = row_divider("Telegram Bot", y)
    panels.append(r)

    telegram_row = [
        timeseries(
            "Inbound Updates (6h)",
            [("sum by (kind, result) (increase(bgw_telegram_update_total[6h]))", "{{kind}} / {{result}}")],
            unit="none",
        ),
        timeseries(
            "Media: voice / photo / reply (6h)",
            [("sum by (kind, result) (increase(bgw_telegram_media_total[6h]))", "{{kind}} / {{result}}")],
            unit="none",
            description=(
                "Inbound voice (STT) + photo (vision) + spoken voice_reply (TTS) outcomes. "
                "voice/result=stt_unreachable is expected when Helios is asleep; "
                "tts_failed / vision_failed / send_failed are actionable."
            ),
        ),
        timeseries(
            "Button Taps: Done / Snooze (6h)",
            [("sum by (action, result) (increase(bgw_telegram_callback_total[6h]))", "{{action}} / {{result}}")],
            unit="none",
            description=(
                "Done/Snooze/selfcare taps. ack/snooze result=not_found or a flat rate while "
                "reminders fire points at taps not landing (the 'Done didn't stick' class)."
            ),
        ),
    ]
    row, y = grid_row(telegram_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Sleep Wind-Down
    # The wind-down ladder (PR #56): T-60 lights rung + T-30 nudge rung ahead
    # of bedtime. The T-30 nudge is watched by the WindDownNudgeStale alert
    # (quiet Pushover) because a missing spoken nudge is invisible by omission;
    # the T-60 lights rung has no alert (a failed dim is self-evident in the
    # house), so its heartbeat panel here is the only place a silently-dropped
    # dim job shows up.
    r, y = row_divider("Sleep Wind-Down", y)
    panels.append(r)

    wind_down_row = [
        stat(
            "Nudge Heartbeat Age (T-30)",
            # The > 0 guard mirrors the briefing stale-alert rules: the gauge is
            # unseeded (default 0) until the job first stamps it, so this shows
            # "No data" instead of a ~56-year orange before the first real run.
            "time() - (bgw_wind_down_last_run_timestamp_seconds > 0)",
            unit="s",
            # 25.5h, not 25h: the largest legitimate gap is 25h (the DST
            # fall-back night) + 300s misfire grace; 25.5h adds ~25m of headroom.
            thresholds=[(None, "green"), (91800, "orange")],
            description="Seconds since the T-30 screens-away nudge job last fired. "
            "Fires nightly; > 25.5h (orange) means the scheduler dropped a rung. "
            "Also alerts (WindDownNudgeStale, quiet Pushover) since a missing "
            "spoken nudge is invisible in the house. NOT seeded at startup (as of "
            "2026-07-08): 'No data' means the job hasn't fired since the last "
            "restart, or wind-down is disabled — the gauge only stamps on a real run.",
        ),
        stat(
            "Dim Heartbeat Age (T-60)",
            # Stamped at the top of wind_down_dim before its early returns, so
            # this ticks even on DND / no-scene nights. Unseeded (default 0)
            # until the first real run, same > 0 guard as the nudge panel.
            "time() - (bgw_wind_down_dim_last_run_timestamp_seconds > 0)",
            unit="s",
            thresholds=[(None, "green"), (91800, "orange")],
            description="Seconds since the T-60 lights-dim job last fired. "
            "Ticks nightly regardless of whether it attempted a scene, so an "
            "orange here means the scheduler dropped ONLY the dim rung while the "
            "Scene Outcomes panel would otherwise stay silently empty. No alert "
            "watches this rung — a failed dim is self-evident in the house. "
            "NOT seeded at startup: 'No data' means the dim job hasn't fired since "
            "the last restart, or wind-down is disabled.",
        ),
        timeseries(
            "Scene Outcomes (T-60 lights, /day)",
            [
                (
                    "sum by (scene, result) (increase(bgw_wind_down_scene_result_total[1d]))",
                    "{{scene}} {{result}}",
                )
            ],
            unit="none",
            description="Rolling 1-day scene activations by outcome (ok | failed | error). "
            "The forensic answer to 'lights didn't dim last night'.",
        ),
        stat(
            "Scene Failures (7d)",
            'sum(increase(bgw_wind_down_scene_result_total{result=~"failed|error"}[7d])) or vector(0)',
            unit="none",
            thresholds=[(None, "green"), (1, "orange")],
            description="Green 0 alongside an empty Scene Outcomes panel means the "
            "T-60 rung isn't attempting scenes (WIND_DOWN_SCENE unset, or the job "
            "isn't firing) — not that scenes are healthy.",
        ),
    ]
    row, y = grid_row(wind_down_row, y, heights=[8, 8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Background Jobs
    r, y = row_divider("Background Jobs", y)
    panels.append(r)

    bg_desc = (
        "Nightly training-corpus drain: in-process counter only — records "
        "written by the out-of-process manual backfill are not counted here. "
        "A flat-zero series on days you used Jess via OWUI / voice means the "
        "drain broke; investigate via the logs row."
    )
    background_row = [
        stat(
            "Training Corpus Records (cumulative, in-process)",
            "sum(bgw_training_corpus_records_total)",
            unit="none",
        ),
        timeseries(
            "Training Corpus New Records (daily, by source)",
            [
                (
                    "sum by (source) (increase(bgw_training_corpus_records_total[1d]))",
                    "{{source}}",
                )
            ],
            unit="none",
            stack=True,
            fill=40,
            description=bg_desc,
        ),
        stat(
            "Training Corpus Records (by source)",
            "sum by (source) (bgw_training_corpus_records_total)",
            unit="none",
            text_mode="value_and_name",
        ),
    ]
    row, y = grid_row(background_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # Scheduler reliability: APScheduler drops any job whose 300s
    # misfire_grace_time lapses during an event-loop stall (shared.py
    # job_defaults). One-shot date jobs (reminder delivery, focus break,
    # dnd_auto_unmute) have no next occurrence, so a drop is a permanently lost
    # action. The EVENT_JOB_MISSED listener (orchestrator.py startup) turns each
    # drop into an ERROR log + a bgw_scheduler_jobs_missed_total bump, collapsed
    # to a bounded job_family label. Any non-zero series here means a job was
    # silently discarded — cross-reference the Errors + Warnings logs panel.
    sched_missed_desc = (
        "Jobs APScheduler dropped after their 300s misfire grace lapsed during an "
        "event-loop stall. One-shot families (reminder, focus) are lost permanently — "
        "there is no runtime re-schedule. Any bar here is a dropped job; 0/flat is healthy."
    )
    scheduler_reliability_row = [
        timeseries(
            "Scheduler Missed Jobs (by family, /day)",
            [
                (
                    "sum by (job_family) (increase(bgw_scheduler_jobs_missed_total[1d]))",
                    "{{job_family}}",
                )
            ],
            unit="none",
            stack=True,
            fill=40,
            thresholds=[(None, "green"), (1, "red")],
            description=sched_missed_desc,
        ),
        stat(
            "Missed Jobs (7d)",
            "sum(increase(bgw_scheduler_jobs_missed_total[7d])) or vector(0)",
            unit="none",
            thresholds=[(None, "green"), (1, "red")],
            description="Total scheduler jobs dropped in the last 7 days across all "
            "families. Non-zero = the event loop stalled past the misfire grace and "
            "silently discarded work.",
        ),
        stat(
            "Missed Jobs (by family, total)",
            "sum by (job_family) (bgw_scheduler_jobs_missed_total)",
            unit="none",
            text_mode="value_and_name",
            thresholds=[(None, "green"), (1, "red")],
            description="Cumulative drops per family since orchestrator start. "
            "Which family is being lost points at where to look first.",
        ),
    ]
    row, y = grid_row(scheduler_reliability_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # -------------------------------------------------------- Logs (with $request_id trace)
    r, y = row_divider("Logs", y)
    panels.append(r)

    logs_row = [
        logs(
            "Trace Request (paste $request_id)",
            '{container="brain-orchestrator"} | json | request_id="$request_id"',
        ),
        logs(
            "Errors + Warnings",
            '{container="brain-orchestrator"} | json | level=~"error|critical|warning"',
        ),
    ]
    row, y = grid_row(logs_row, y, heights=[10, 10])
    panels.extend(row)

    assign_ids(panels)

    templating = [
        var_query("tool", "Tool", "label_values(bgw_tool_calls_total, tool)"),
        var_query("mode", "Mode", "label_values(bgw_requests_total, mode)"),
        var_query("speaker", "Speaker", "label_values(bgw_tts_announcements_total, speaker)"),
        var_query("wing", "Palace Wing", "label_values(bgw_palace_stores_total, wing)"),
        var_text("request_id", "Request ID"),
    ]

    return dashboard(
        title="Brain Gateway",
        uid="brain-gateway-overview",
        description=(
            "Full application telemetry for Brain Gateway — the single canonical "
            "app dashboard (merged from the former Overview + SRE + Deep Dive). "
            "Every metric in orchestrator/metrics.py is covered here, grouped by "
            "subsystem. Infra lives in the Homelab Infrastructure dashboard. "
            "Use filters at the top to drill into a specific tool, mode, "
            "speaker, or palace wing. Paste a request_id into the text "
            "variable to trace a single request end-to-end in the logs row. "
            "Generated — edit monitoring/grafana/dashgen/dashboards/brain_gateway_sre.py, not the JSON."
        ),
        tags=["brain-gateway", "sre"],
        refresh="30s",
        time_from="now-3h",
        templating=templating,
        panels=panels,
    )
